import asyncio
import tempfile
from pathlib import Path
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.devlab import protocol as S

async def test():
    with tempfile.TemporaryDirectory() as tmp:
        store = HubStore(Path(tmp))
        await store.open()
        await store.queue.add_project('demo')
        
        a = await store.queue.submit('client', 'demo', 'model', 'task', depends_on=[])
        b = await store.queue.submit('client', 'demo', 'model', 'task', depends_on=[a.id])
        c = await store.queue.submit('client', 'demo', 'model', 'task', depends_on=[])
        
        print(f'a={a.id}, b={b.id}, c={c.id}')
        print(f'a status={a.status}, b status={b.status}, c status={c.status}')
        print(f'b params={b.params}')
        
        # Check the raw params in DB
        async with store._db.transaction() as conn:
            cur = await conn.execute("SELECT id, params FROM jobs WHERE client = ?", ('client',))
            rows = await cur.fetchall()
            for r in rows:
                print(f'  job {r["id"]}: params={r["params"]}')
        
        # Check the query with json_each
        async with store._db.transaction() as conn:
            cur = await conn.execute(
                """SELECT j.id, d.value FROM jobs j, json_each(j.params, '$.depends_on') d 
                WHERE j.client = ?""",
                ('client',),
            )
            rows = await cur.fetchall()
            print(f'json_each results: {[(r["id"], r["value"]) for r in rows]}')
        
        await store.close()

asyncio.run(test())
