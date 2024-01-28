import os
import sys
import gzip
import time
import json
import hmac
import asyncio
import sqlite3
import logging
import httprpc
import hashlib
import argparse
from logging import critical as log


async def fetch(ctx, db, key=None):
    db = os.path.join('paxosdb', db + '.sqlite3')
    if not os.path.isfile(db):
        raise Exception('NOT_INITIALIZED')

    db = sqlite3.connect(db)
    try:
        if key is None:
            # All accepted keys
            return db.execute('''select key, version from paxos
                                 where accepted_seq > 0
                              ''').fetchall()
        else:
            # Most recent version of this key
            return db.execute('''select version, value from paxos
                                 where key=? and accepted_seq > 0
                                 order by version desc limit 1
                              ''', [key]).fetchone()
    finally:
        db.close()


async def paxos_server(ctx, db, key, version, proposal_seq, octets=None):
    version = int(version)
    proposal_seq = int(proposal_seq)

    if time.time() > proposal_seq + 10 or time.time() < proposal_seq - 10:
        # For liveness - out of sync clocks can block further rounds
        raise Exception('CLOCKS_OUT_OF_SYNC')

    if not ctx.get('subject', ''):
        raise Exception('TLS_AUTH_FAILED')

    os.makedirs('paxosdb', exist_ok=True)
    db = sqlite3.connect(os.path.join('paxosdb', db + '.sqlite3'))
    try:
        db.execute('''create table if not exists paxos(
                          key          text,
                          version      int,
                          promised_seq int,
                          accepted_seq int,
                          value        blob,
                          primary key(key, version)
                      )''')

        db.execute('insert or ignore into paxos values(?,?,0,0,null)',
                   [key, version])

        if octets is None:
            # Paxos PROMISE - Block stale writers and return the most recent
            # accepted value. Client will propose the most recent across
            # servers in the accept phase
            promised_seq, accepted_seq, value = db.execute(
                '''select promised_seq, accepted_seq, value
                   from paxos where key=? and version=?
                ''', [key, version]).fetchone()

            if proposal_seq > promised_seq:
                db.execute('''update paxos set promised_seq=?
                              where key=? and version=?
                           ''', [proposal_seq, key, version])
                db.commit()

                # CRUX of the paxos protocol - return the accepted value
                return dict(accepted_seq=accepted_seq, value=value)
        else:
            # Paxos ACCEPT - Client has sent the most recent value from the
            # promise phase.
            promised_seq = db.execute(
                'select promised_seq from paxos where key=? and version=?',
                [key, version]).fetchone()[0]

            if proposal_seq >= promised_seq:
                db.execute(
                    '''update paxos set promised_seq=?, accepted_seq=?, value=?
                       where key=? and version=?
                    ''', [proposal_seq, proposal_seq, octets, key, version])

                # Delete older values of this key.
                # This is unrelated to and does not impact Paxos steps.
                db.execute(
                    '''delete from paxos where key=? and version < (
                           select max(version) from paxos
                           where key=? and accepted_seq > 0)
                    ''', [key, key])

                return db.commit()
    finally:
        db.rollback()
        db.close()

    raise Exception(f'STALE_PROPOSAL_SEQ {key}:{version} {proposal_seq}')


async def paxos_client(rpc, db, key, version, obj=b''):
    seq = int(time.time())  # Current timestamp is a good enough seq
    url = f'db/{db}/key/{key}/version/{version}/proposal_seq/{seq}'
    version = int(version)

    if obj != b'':
        # value to be set should always be json serializable
        octets = gzip.compress(json.dumps(obj).encode())

    # Paxos PROMISE phase - block stale writers
    accepted_seq = 0
    for v in await rpc.quorum_invoke(f'promise/{url}'):
        # CRUX of the paxos protocol - Find the most recent accepted value
        if v['accepted_seq'] > accepted_seq:
            accepted_seq, octets = v['accepted_seq'], v['value']

    # Paxos ACCEPT phase - propose the value found above
    await rpc.quorum_invoke(f'accept/{url}', octets)


async def get(ctx, db, key=None):
    rpc = ctx.get('rpc', RPCClient(G.cert, G.cert, G.servers))

    if key is None:
        keys = dict()
        for values in await rpc.quorum_invoke(f'fetch/db/{db}'):
            for key, version in values:
                if key not in keys or version > keys[key]:
                    keys[key] = version

        return dict(db=db, keys=keys)
    else:
        for i in range(rpc.quorum):
            vlist = await rpc.quorum_invoke(f'fetch/db/{db}/key/{key}')

            if all([vlist[0] == v for v in vlist]):
                if vlist[0] is None:
                    return dict(db=db, key=key, version=None)

                return dict(
                    db=db, key=key, version=vlist[0][0],
                    value=json.loads(gzip.decompress(vlist[0][1]).decode()))

            version = max([v[0] for v in vlist if v and v[0] is not None])
            await paxos_client(rpc, db, key, version)


def get_hmac(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()


async def put(ctx, db, secret, key, version, obj):
    ctx['rpc'] = RPCClient(G.cert, G.cert, G.servers)

    res = await get(ctx, db, '#')
    if res['value'] == get_hmac(secret, db):
        # Update and return the most recent version. Most recent version could
        # be higher than what we requested if there was a newer request before
        # this completed. Even if the version is same, it could be a different
        # value set by another parallel request.
        #
        # Paxos guarantees that the value for the returned version is now
        # final and would not change under any condition.
        await paxos_client(ctx['rpc'], db, key, version, obj)
        return await get(ctx, db, key)

    raise Exception('Authentication Failed')


# Initialize the db and generate api key
async def init(ctx, db, secret, new_secret=None):
    ctx['rpc'] = RPCClient(G.cert, G.cert, G.servers)

    if new_secret:
        # DB exists. Just change the password
        obj = get_hmac(new_secret, db)
        res = await get(ctx, db, '#')
        res = await put(ctx, db, secret, '#', res['version'] + 1, obj)
    else:
        # Create a new db
        obj = get_hmac(secret, db)
        res = await paxos_client(ctx['rpc'], db, '#', 0, obj)
        res = await get(ctx, db, '#')

    return dict(db=db, version=res['version'],
                status='OK' if obj == res['value'] else 'CONFLICT')


class RPCClient(httprpc.Client):
    def __init__(self, cacert, cert, servers):
        super().__init__(cacert, cert, servers)

    async def quorum_invoke(self, resource, octets=b''):
        res = await self.cluster(resource, octets)
        result = list()

        exceptions = list()
        for s, r in zip(self.conns.keys(), res):
            if isinstance(r, Exception):
                log(f'{s} {type(r)} {r}')
                exceptions.append(f'\n-{s}\n{r}')
            else:
                result.append(r)

        if len(result) < self.quorum:
            raise Exception('\n'.join(exceptions))

        return result


if '__main__' == __name__:
    logging.basicConfig(format='%(asctime)s %(process)d : %(message)s')

    P = argparse.ArgumentParser()
    P.add_argument('--cert', help='certificate path')
    P.add_argument('--port', type=int, help='port number for server')
    P.add_argument('--servers', help='comma separated list of server ip:port')
    P.add_argument('--db', help='db for get/put')
    P.add_argument('--key', help='key for get/put')
    P.add_argument('--version', type=int, help='version for put')
    G = P.parse_args()

    if G.port:
        httprpc.run(G.port, dict(init=init, get=get, put=put, fetch=fetch,
                                 promise=paxos_server, accept=paxos_server),
                    cacert=G.cert, cert=G.cert)
    elif G.db and G.key and G.version is not None:
        asyncio.run(paxos_client(RPCClient(G.cert, G.cert, G.servers),
                                 G.db, G.key, G.version,
                                 json.loads(sys.stdin.read())))
        print(json.dumps(asyncio.run(get(dict(), G.db, G.key)),
                         sort_keys=True, indent=4))
    elif G.db:
        print(json.dumps(asyncio.run(get(dict(), G.db, G.key)),
                         sort_keys=True, indent=4))
    else:
        P.print_help()
        exit(1)