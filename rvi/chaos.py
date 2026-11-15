"""Network chaos proxy: a seeded TCP proxy that emulates 20%-100% signal strength.

It sits between client and backend. A `Profile` gives the signal strength at
time t (seconds since the FIRST client connection, so a scenario's timeline does
not depend on how long the client spends hashing/probing before it connects);
strength is mapped to impairments:

    bandwidth   = max_bw * strength^2          (weak signal -> much lower throughput)
    latency     = base + (1 - strength) * 400 ms per connection
    stall       = chance a connection freezes (longer than the client timeout)
    reset       = chance a connection is cut mid-transfer
    blackout    = strength 0: every connection is dropped (temporary disconnection)
    corrupt     = optional: flip bytes of upload payloads in transit

Limitation (stated honestly): this works at the TCP stream level, so packet loss
is emulated by its application-visible effects (stalls, resets, slowdowns), not
by dropping individual IP packets. Use `tc netem` on Linux for packet-level loss.
"""
import argparse
import random
import socket
import threading
import time


class Profile:
    """Piecewise-constant signal strength. segments = [(duration_s, strength), ...]."""

    def __init__(self, segments, loop=True):
        self.segments = segments
        self.loop = loop
        self.total = sum(d for d, _ in segments)

    def strength(self, t):
        if self.loop and self.total > 0:
            t = t % self.total
        for d, s in self.segments:
            if t < d:
                return s
            t -= d
        return self.segments[-1][1]

    @classmethod
    def constant(cls, s):
        return cls([(1.0, s)])

    @classmethod
    def fluctuating(cls, seed, lo=0.2, hi=1.0, step=0.5, n=240):
        rng = random.Random(seed)
        return cls([(step, round(rng.uniform(lo, hi), 2)) for _ in range(n)])


class ChaosProxy:
    def __init__(self, listen_port, upstream, profile=None, seed=0, max_bw=3_000_000,
                 stall_rate=0.0, reset_rate=0.0, corrupt_rate=0.0, stall_seconds=30.0,
                 fault_window=200_000, host="127.0.0.1"):
        self.host = host
        self.port = listen_port
        self.upstream = upstream  # (host, port)
        self.profile = profile or Profile.constant(1.0)
        self.rng = random.Random(seed)
        self.rng_lock = threading.Lock()
        self.max_bw = max_bw
        self.stall_rate = stall_rate
        self.reset_rate = reset_rate
        self.corrupt_rate = corrupt_rate
        self.stall_seconds = stall_seconds
        self.fault_window = fault_window  # a planned stall/reset fires within this many upload bytes
        self.t0 = None  # profile clock; armed by the first client connection
        self.stats = {"connections": 0, "blackout_drops": 0, "stalls": 0, "resets": 0,
                      "corrupted_bytes": 0, "bytes_up": 0, "bytes_down": 0}
        self.trace = []  # (t, strength) samples for the evidence log
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, listen_port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(64)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def elapsed(self):
        return time.time() - self.t0 if self.t0 is not None else 0.0

    def strength(self):
        return self.profile.strength(self.elapsed())

    def _rand(self):
        with self.rng_lock:
            return self.rng.random()

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.t0 is None:
                self.t0 = time.time()
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        self.stats["connections"] += 1
        s = self.strength()
        self.trace.append((round(self.elapsed(), 2), s))
        if s <= 0.0:  # blackout: the network is simply gone
            self.stats["blackout_drops"] += 1
            c.close()
            return
        weak = 1.0 - s
        time.sleep(0.01 + weak * 0.4)  # latency grows as the signal weakens
        plan = {"stall_at": None, "reset_at": None}
        if self._rand() < self.stall_rate * weak:
            plan["stall_at"] = int(self._rand() * self.fault_window)
        elif self._rand() < self.reset_rate * weak:
            plan["reset_at"] = int(self._rand() * self.fault_window)
        try:
            u = socket.create_connection(self.upstream, timeout=5)
        except OSError:
            c.close()
            return
        dead = threading.Event()
        a = threading.Thread(target=self._pump, args=(c, u, True, plan, dead), daemon=True)
        b = threading.Thread(target=self._pump, args=(u, c, False, plan, dead), daemon=True)
        a.start(); b.start()
        a.join(); b.join()
        for x in (c, u):
            try:
                x.close()
            except OSError:
                pass

    def _pump(self, src, dst, upward, plan, dead):
        seen = 0
        try:
            while not dead.is_set() and not self._stop.is_set():
                s = self.strength()
                if s <= 0.0:
                    self.stats["blackout_drops"] += 1
                    break
                bw = max(8_000.0, self.max_bw * s * s)  # bytes/second
                src.settimeout(0.5)
                try:
                    data = src.recv(min(16384, max(1024, int(bw * 0.05))))
                except socket.timeout:
                    continue
                if not data:
                    break
                if upward:
                    if plan["stall_at"] is not None and seen + len(data) > plan["stall_at"]:
                        self.stats["stalls"] += 1
                        dead.wait(self.stall_seconds)  # freeze: the client must time out
                        break
                    if plan["reset_at"] is not None and seen + len(data) > plan["reset_at"]:
                        self.stats["resets"] += 1
                        break
                    # Only damage payload bytes, never the HTTP header block.
                    if self.corrupt_rate and seen > 2048 and self._rand() < self.corrupt_rate:
                        i = int(self._rand() * len(data))
                        data = data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1:]
                        self.stats["corrupted_bytes"] += 1
                seen += len(data)
                self.stats["bytes_up" if upward else "bytes_down"] += len(data)
                time.sleep(len(data) / bw)  # throttle to the current bandwidth
                dst.sendall(data)
        except OSError:
            pass
        finally:
            dead.set()
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Network chaos proxy (20%-100% signal emulation)")
    ap.add_argument("--listen", type=int, default=9000)
    ap.add_argument("--upstream", default="127.0.0.1:8000")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--strength", type=float, help="constant strength 0.2-1.0 (default: fluctuating)")
    ap.add_argument("--max-bw", type=int, default=3_000_000, help="bytes/sec at 100%% strength")
    ap.add_argument("--stall-rate", type=float, default=0.15)
    ap.add_argument("--reset-rate", type=float, default=0.15)
    ap.add_argument("--corrupt-rate", type=float, default=0.0)
    a = ap.parse_args()
    h, p = a.upstream.split(":")
    prof = Profile.constant(a.strength) if a.strength else Profile.fluctuating(a.seed)
    px = ChaosProxy(a.listen, (h, int(p)), prof, a.seed, a.max_bw, a.stall_rate, a.reset_rate, a.corrupt_rate).start()
    print("chaos proxy :%d -> %s  (seed=%d)" % (a.listen, a.upstream, a.seed))
    try:
        while True:
            time.sleep(1)
            print("  strength=%.0f%%  %s" % (px.strength() * 100, px.stats), end="\r")
    except KeyboardInterrupt:
        px.stop()


if __name__ == "__main__":
    main()
