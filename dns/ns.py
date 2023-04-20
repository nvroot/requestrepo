#!/usr/bin/env python3
import datetime
import os
from time import sleep
import random
import threading

from dnslib import DNSLabel, QTYPE, RD, RR, RCODE
from dnslib import A, AAAA, CNAME, MX, NS, SOA, TXT
from dnslib.server import DNSServer
from mongolog import insert_into_db, update_dns_record, get_dns_record
from config import config
from utils import get_subdomain


EPOCH = datetime.datetime(1970, 1, 1)
SERIAL = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

TYPE_LOOKUP = {
    A: QTYPE.A,
    AAAA: QTYPE.AAAA,
    CNAME: QTYPE.CNAME,
    MX: QTYPE.MX,
    NS: QTYPE.NS,
    SOA: QTYPE.SOA,
    TXT: QTYPE.TXT,
}


class Record:
    def __init__(self, rdata_type, *args, rtype=None, rname=None, ttl=None, **kwargs):
        if isinstance(rdata_type, RD):
            self._rtype = TYPE_LOOKUP[rdata_type.__class__]
            rdata = rdata_type
        else:
            self._rtype = TYPE_LOOKUP[rdata_type]
            if rdata_type == SOA and len(args) == 2:
                args += (
                    (
                        SERIAL,  # serial number
                        60 * 60 * 1,  # refresh
                        60 * 60 * 3,  # retry
                        60 * 60 * 24,  # expire
                        60 * 60 * 1,  # minimum
                    ),
                )
            rdata = rdata_type(*args)

        if rtype:
            self._rtype = rtype
        self._rname = rname
        self.kwargs = dict(
            rdata=rdata, ttl=self.sensible_ttl() if ttl is None else ttl, **kwargs
        )

    def try_rr(self, q):
        if q.qtype == QTYPE.ANY or q.qtype == self._rtype:
            return self.as_rr(q.qname)

    def as_rr(self, alt_rname):
        return RR(rname=self._rname or alt_rname, rtype=self._rtype, **self.kwargs)

    def sensible_ttl(self):
        return 1

    @property
    def is_soa(self):
        return self._rtype == QTYPE.SOA

    def __str__(self):
        return "{} {}".format(QTYPE[self._rtype], self.kwargs)


def save_into_db(reply, ip, raw):
    name = str(reply.q.qname)
    uid = get_subdomain(name)

    if not uid:
        return

    data = {
        "date": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "ip": ip,
        "type": QTYPE[reply.q.qtype],
        "name": name,
        "uid": uid,
        "reply": str(reply),
        "raw": raw,
    }
    insert_into_db(data)


class Resolver:
    def __init__(self, server_ip: str, server_domain: str):
        self.server_ip = server_ip
        self.server_domain = server_domain + "."

    def resolve_cname(self, reply):
        data = get_dns_record(str(reply.q.qname), "CNAME")
        if data == None:
            return Record(CNAME, self.server_domain)
        else:
            return Record(CNAME, data["value"])

    def resolve_txt(self, reply):
        data = get_dns_record(str(reply.q.qname), "TXT")
        if data == None:
            return Record(TXT, os.getenv("TXT") or "Hello!")
        else:
            return Record(TXT, data["value"])

    def resolve_ip(self, reply, dtype):
        new_record = None
        data = get_dns_record(str(reply.q.qname), dtype)
        if data == None:
            new_record = Record(A if dtype == "A" else AAAA, self.server_ip)
        else:
            ips = data["value"]
            if "/" not in ips and "%" not in ips:
                new_record = Record(A, ips)
            else:
                if "%" in ips:
                    ips = ips.split("%")
                    idx = random.randint(0, len(ips) - 1)
                    if "/" in ips[idx]:
                        new_ips = ips[idx].split("/")
                        new_record = Record(A, new_ips[0])
                        new_ips = "/".join(new_ips[1:] + [new_ips[0]])
                        ips[idx] = new_ips
                        ips = "%".join(ips)
                        update_dns_record(data["subdomain"], data["domain"], "A", ips)
                    else:
                        new_record = Record(A, ips[idx])
                else:
                    ips = ips.split("/")
                    new_record = Record(A, ips[0])
                    ips = "/".join(ips[1:] + [ips[0]])
                    update_dns_record(data["subdomain"], data["domain"], "A", ips)

        return new_record

    def resolve(self, request, handler):
        reply = request.reply()

        # We assume that the data in the DB is correct (using server side checks)
        new_record = None

        if QTYPE[reply.q.qtype] == "CNAME":
            new_record = self.resolve_cname(reply)
        elif QTYPE[reply.q.qtype] == "TXT":
            new_record = self.resolve_txt(reply)
        elif QTYPE[reply.q.qtype] == "A":
            new_record = self.resolve_ip(reply, "A")
        elif QTYPE[reply.q.qtype] == "AAAA":
            new_record = self.resolve_ip(reply, "AAAA")

        if new_record != None:
            reply.add_answer(new_record.try_rr(request.q))
            try:
                save_into_db(reply, handler.client_address[0], handler.request[0])
            except Exception as ex:
                print(ex)
                pass

        return reply


resolver = Resolver(config.server_ip, config.server_domain)
servers = [
    DNSServer(resolver, port=53, address="0.0.0.0", tcp=True),
    DNSServer(resolver, port=53, address="0.0.0.0", tcp=False),
]

if __name__ == "__main__":
    stop_event = threading.Event()

    for s in servers:
        s.start_thread()

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.stop()
