"""아파트 단지 좋아요 — Valkey(Redis 호환) 저장 + pub/sub 실시간 알림.

- 외부 패키지 없이 표준 라이브러리 소켓으로 RESP 프로토콜을 직접 말한다(필요한 명령만).
- 키 구조
    hf:like:{apt_id}   SET  — 좋아요를 누른 익명 브라우저 ID
    hf:likes           ZSET — apt_id → 좋아요 수 (인기 순위)
    hf:aptname         HASH — apt_id → "구 동 단지명" (순위 표시용)
    hf:trend:like:{YYYYMMDDHH}  ZSET — 그 시간에 늘어난 좋아요(취소는 −1), 30시간 뒤 자동 삭제
    hf:trend:view:{YYYYMMDDHH}  ZSET — 그 시간의 상세 열람 수(같은 브라우저·단지는 시간당 1회)
    채널 hf:likes:events — {"apt_id","count"} 를 발행
- 익명 ID 는 브라우저가 만든 무작위 문자열이며 고객 데이터와 연결하지 않는다.
"""
import json
import os
import time
import re
import socket
import threading

HOST = os.environ.get("HOMEFIT_VALKEY_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOMEFIT_VALKEY_PORT", "6379"))
CHANNEL = "hf:likes:events"
VOTER_RE = re.compile(r"^[a-z0-9]{16,40}$")
APT_RE = re.compile(r"^[0-9a-f]{10}$")


TREND_TTL = 30 * 3600


def _hour(ts=None):
    return time.strftime("%Y%m%d%H", time.gmtime(ts if ts is not None else time.time()))


class RespError(Exception):
    pass


def _enc(*args):
    out = [f"*{len(args)}\r\n".encode()]
    for a in args:
        b = a if isinstance(a, bytes) else str(a).encode("utf-8")
        out.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
    return b"".join(out)


class Conn:
    """한 연결 = 한 소켓. 명령 연결은 잠금으로 직렬화해 스레드 사이에서 함께 쓴다."""

    def __init__(self, timeout=3.0):
        self.sock = socket.create_connection((HOST, PORT), timeout=timeout)
        self.f = self.sock.makefile("rb")
        self.lock = threading.Lock()

    def _read(self):
        line = self.f.readline()
        if not line:
            raise ConnectionError("valkey connection closed")
        t, rest = line[:1], line[1:-2]
        if t == b"+":
            return rest.decode()
        if t == b"-":
            raise RespError(rest.decode())
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = self.f.read(n + 2)[:-2]
            return data.decode("utf-8")
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self._read() for _ in range(n)]
        raise RespError(f"unknown reply {t!r}")

    def cmd(self, *args):
        with self.lock:
            self.sock.sendall(_enc(*args))
            return self._read()

    def pipeline(self, cmds):
        with self.lock:
            self.sock.sendall(b"".join(_enc(*c) for c in cmds))
            return [self._read() for _ in cmds]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Likes:
    def __init__(self):
        self._c = None
        self._lock = threading.Lock()

    def _conn(self):
        with self._lock:
            if self._c is None:
                self._c = Conn()
            return self._c

    def _call(self, fn):
        """연결이 끊겼으면 한 번 다시 붙는다."""
        try:
            return fn(self._conn())
        except (OSError, ConnectionError):
            with self._lock:
                if self._c:
                    self._c.close()
                self._c = None
            return fn(self._conn())

    def available(self):
        try:
            return self._call(lambda c: c.cmd("PING")) == "PONG"
        except (OSError, ConnectionError, RespError):
            return False

    def counts(self, apt_ids, voter=None):
        ids = [a for a in apt_ids if APT_RE.match(a)][:200]
        if not ids:
            return {}
        def run(c):
            cmds = [("ZSCORE", "hf:likes", a) for a in ids]
            if voter:
                cmds += [("SISMEMBER", f"hf:like:{a}", voter) for a in ids]
            return c.pipeline(cmds)
        r = self._call(run)
        n = len(ids)
        return {a: {"count": int(float(r[i] or 0)), "mine": bool(r[n + i]) if voter else False} for i, a in enumerate(ids)}

    def toggle(self, apt_id, voter, on, label):
        if not APT_RE.match(apt_id) or not VOTER_RE.match(voter):
            raise ValueError("잘못된 요청이에요")
        key = f"hf:like:{apt_id}"
        def run(c):
            changed = c.cmd("SADD" if on else "SREM", key, voter)
            if changed:
                c.cmd("ZINCRBY", "hf:likes", 1 if on else -1, apt_id)
                hk = f"hf:trend:like:{_hour()}"
                c.pipeline([("ZINCRBY", hk, 1 if on else -1, apt_id), ("EXPIRE", hk, TREND_TTL)])
            if label:
                c.cmd("HSET", "hf:aptname", apt_id, label)
            cnt = c.cmd("SCARD", key)
            c.cmd("ZADD", "hf:likes", cnt, apt_id)          # 집합 크기로 맞춰 둔다(경쟁 상황 보정)
            if changed:
                c.cmd("PUBLISH", CHANNEL, json.dumps({"apt_id": apt_id, "count": cnt}))
            return cnt
        cnt = self._call(run)
        return {"apt_id": apt_id, "count": int(cnt), "mine": on}

    def view(self, apt_id, voter, label=None):
        """상세 열람 기록. 같은 브라우저가 같은 단지를 한 시간에 여러 번 열어도 1회."""
        if not APT_RE.match(apt_id) or not VOTER_RE.match(voter):
            raise ValueError("잘못된 요청이에요")
        h = _hour()
        def run(c):
            first = c.cmd("SET", f"hf:viewed:{voter}:{apt_id}:{h}", 1, "NX", "EX", 3600)
            if first == "OK":
                vk = f"hf:trend:view:{h}"
                c.pipeline([("ZINCRBY", vk, 1, apt_id), ("EXPIRE", vk, TREND_TTL)])
                if label:
                    c.cmd("HSET", "hf:aptname", apt_id, label)
            return first == "OK"
        return self._call(run)

    def trending(self, n=10, hours=24, decay=0.9, view_weight=0.2):
        """최근 hours 시간 급상승 점수 = Σ decay^경과시간 × (좋아요 증가 + view_weight × 열람)."""
        now = time.time()
        keys = [(_hour(now - 3600 * k), decay ** k) for k in range(hours)]
        def run(c):
            cmds = [("ZRANGE", f"hf:trend:like:{h}", 0, -1, "WITHSCORES") for h, _ in keys] + \
                   [("ZRANGE", f"hf:trend:view:{h}", 0, -1, "WITHSCORES") for h, _ in keys]
            return c.pipeline(cmds)
        res = self._call(run)
        score, likes24, views24 = {}, {}, {}
        for i, (_, w) in enumerate(keys):
            for kind, flat in (("like", res[i]), ("view", res[len(keys) + i])):
                for j in range(0, len(flat or []), 2):
                    a, v = flat[j], float(flat[j + 1])
                    if kind == "like":
                        likes24[a] = likes24.get(a, 0) + v
                        score[a] = score.get(a, 0) + w * v
                    else:
                        views24[a] = views24.get(a, 0) + v
                        score[a] = score.get(a, 0) + w * view_weight * v
        top = sorted((a for a in score if score[a] > 0), key=lambda a: -score[a])[:n]
        totals = self._call(lambda c: c.pipeline([("ZSCORE", "hf:likes", a) for a in top])) if top else []
        return [{"apt_id": a, "score": round(score[a], 2), "likes_24h": int(likes24.get(a, 0)), "views_24h": int(views24.get(a, 0)),
                 "likes": int(float(t or 0))} for a, t in zip(top, totals)]

    def top(self, n=10):
        def run(c):
            flat = c.cmd("ZREVRANGE", "hf:likes", 0, n - 1, "WITHSCORES") or []
            pairs = [(flat[i], int(float(flat[i + 1]))) for i in range(0, len(flat), 2)]
            pairs = [p for p in pairs if p[1] > 0]
            labels = c.pipeline([("HGET", "hf:aptname", a) for a, _ in pairs]) if pairs else []
            return [{"apt_id": a, "count": k, "label": lb} for (a, k), lb in zip(pairs, labels)]
        return self._call(run)


def subscribe():
    """pub/sub 전용 새 연결을 만들어 (연결, 메시지 반복자)를 돌려준다. 호출한 쪽이 연결을 닫는다."""
    c = Conn(timeout=None)
    c.sock.sendall(_enc("SUBSCRIBE", CHANNEL))
    c._read()                                       # 구독 확인 응답

    def messages():
        while True:
            m = c._read()
            if isinstance(m, list) and len(m) == 3 and m[0] == "message":
                yield m[2]
    return c, messages()
