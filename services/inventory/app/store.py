"""Event catalog and seat counters in Redis.

Keys:
  events                 set of event ids
  event:{id}             hash  name, date, total
  seats:{id}             int   seats left
  resv:{booking_id}      hash  event, qty, state (reserved|released)  -> idempotency record

Reserve/release are Lua scripts: Redis runs them atomically, so two clients can never
both take the last seat, and replaying the same booking_id never reserves twice.
"""
import redis

RESERVE_LUA = """
local state = redis.call('HGET', KEYS[2], 'state')
if state == 'reserved' then return 1 end          -- same booking_id replayed: already done
if state == 'released' then return -2 end         -- booking_id already used and released
local left = tonumber(redis.call('GET', KEYS[1]))
if left == nil then return -1 end                 -- unknown event
local q = tonumber(ARGV[1])
if left < q then return 0 end                     -- sold out
redis.call('DECRBY', KEYS[1], q)
redis.call('HSET', KEYS[2], 'event', ARGV[2], 'qty', q, 'state', 'reserved')
return 1
"""

RELEASE_LUA = """
local state = redis.call('HGET', KEYS[2], 'state')
if state == false then return 0 end               -- nothing was reserved: nothing to give back
if state == 'released' then return 1 end          -- already released: idempotent
local q = tonumber(redis.call('HGET', KEYS[2], 'qty'))
redis.call('INCRBY', KEYS[1], q)
redis.call('HSET', KEYS[2], 'state', 'released')
return 1
"""

SEED = [
    ("evt-1", "Concert - Le Bikini", "2027-03-14", 200),
    ("evt-2", "Match - Stade Toulousain", "2027-03-21", 500),
    ("evt-3", "Conférence DevOps", "2027-04-02", 50),
]


class Store:
    def __init__(self, client: redis.Redis):
        self.r = client
        self._reserve = client.register_script(RESERVE_LUA)
        self._release = client.register_script(RELEASE_LUA)

    def ping(self):
        return self.r.ping()

    def seed(self):
        for eid, name, date, total in SEED:
            if self.r.sadd("events", eid):  # only if new
                self.r.hset(f"event:{eid}", mapping={"name": name, "date": date, "total": total})
                self.r.set(f"seats:{eid}", total)

    def get_event(self, eid):
        data = self.r.hgetall(f"event:{eid}")
        if not data:
            return None
        left = self.r.get(f"seats:{eid}")
        return {"id": eid, "name": data["name"], "date": data["date"],
                "total": int(data["total"]), "seats_left": int(left or 0)}

    def list_events(self):
        return [e for eid in sorted(self.r.smembers("events")) if (e := self.get_event(eid))]

    def reserve(self, eid, booking_id, qty):
        return int(self._reserve(keys=[f"seats:{eid}", f"resv:{booking_id}"], args=[qty, eid]))

    def release(self, eid, booking_id):
        return int(self._release(keys=[f"seats:{eid}", f"resv:{booking_id}"]))
