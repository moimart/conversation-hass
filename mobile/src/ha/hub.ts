// Client side of the HA bridge (server/app/ha_bridge.py), ported from
// docker-home's HaHub. Instead of an SSE stream per entity set, everything rides
// the satellite's existing /ws/ui socket, which app.js shares via window.HALWs:
//   → ha_retain {ids}            the entities currently on screen
//   ← ha_states / ha_state       their state (initial snapshot / live changes)
//   → ha_subscribe {req, msg}    relayed subscriptions (custom cards)
//   ← ha_subscribed {req, sub} / ha_sub {sub, event}
// Listeners are PER ENTITY so a card re-renders only when its own state moves.

import type { HaState } from "./api";

interface HalWs {
  send(msg: unknown): boolean;
  isOpen(): boolean;
  listen(l: { onOpen?: () => void; onMessage?: (m: any) => void }): () => void;
}

type Listener = () => void;
interface Sub { msg: Record<string, unknown>; cb: (ev: unknown) => void; server: number | null; closed: boolean }

class HaHub {
  readonly states = new Map<string, HaState | null>();
  connected = false;
  private refs = new Map<string, number>();
  private byEntity = new Map<string, Set<Listener>>();
  private anyListeners = new Set<Listener>();
  private retainTimer: number | undefined;
  private nextReq = 1;
  private pendingSubs = new Map<number, { resolve: (id: number) => void; reject: (e: Error) => void }>();
  // Relayed subscriptions keyed by a STABLE local id (the server's id changes on
  // every reconnect); byServer maps the current server id back to it.
  private subs = new Map<number, Sub>();
  private byServer = new Map<number, Sub>();
  private early = new Map<number, unknown[]>();
  private ws: HalWs | null = null;

  attach(): void {
    if (this.ws) return;
    const ws = (window as unknown as { HALWs?: HalWs }).HALWs;
    if (!ws) { console.warn("[ha] HALWs missing — cards get no live state"); return; }
    this.ws = ws;
    ws.listen({
      onOpen: () => this.onReconnect(),
      onMessage: (m) => this.onMessage(m),
    });
    if (ws.isOpen()) this.onReconnect();
  }

  /** Re-announce everything after (re)connecting: the server forgot this device. */
  private onReconnect(): void {
    this.sendRetain();
    this.byServer.clear();
    for (const sub of this.subs.values()) {
      sub.server = null;
      this.establish(sub).catch((e) => console.warn("[ha] resubscribe failed", e));
    }
  }

  private onMessage(m: any): void {
    switch (m.type) {
      case "ha_states":
      case "ha_state": {
        if (typeof m.connected === "boolean") this.connected = m.connected;
        else this.connected = true;
        const changed: string[] = [];
        for (const [id, st] of Object.entries(m.states || {})) {
          this.states.set(id, (st as HaState | null) ?? null);
          changed.push(id);
        }
        this.emit(changed);
        break;
      }
      case "ha_subscribed": {
        const p = this.pendingSubs.get(m.req);
        if (!p) return;
        this.pendingSubs.delete(m.req);
        if (m.error) p.reject(new Error(m.error));
        else p.resolve(m.sub);
        break;
      }
      case "ha_sub": {
        const sub = this.byServer.get(m.sub);
        if (sub) sub.cb(m.event);
        else {
          // An event can beat our bookkeeping of its id; hold it briefly.
          this.early.set(m.sub, [...(this.early.get(m.sub) || []), m.event]);
          setTimeout(() => this.early.delete(m.sub), 10000);
        }
        break;
      }
    }
  }

  private emit(ids: string[]): void {
    const fired = new Set<Listener>();
    for (const id of ids) for (const fn of this.byEntity.get(id) || []) fired.add(fn);
    for (const fn of this.anyListeners) fired.add(fn);
    for (const fn of fired) {
      try { fn(); } catch (e) { console.error("[ha] listener failed", e); }
    }
  }

  /** Call `fn` whenever any of `ids` changes. Also retains them on the server. */
  watch(ids: string[], fn: Listener): () => void {
    const clean = [...new Set(ids.filter(Boolean))];
    for (const id of clean) {
      let set = this.byEntity.get(id);
      if (!set) this.byEntity.set(id, (set = new Set()));
      set.add(fn);
    }
    const release = this.retain(clean);
    return () => {
      for (const id of clean) this.byEntity.get(id)?.delete(fn);
      release();
    };
  }

  /** Every state change (used by custom cards, which may read any entity). */
  watchAll(fn: Listener): () => void {
    this.anyListeners.add(fn);
    return () => this.anyListeners.delete(fn);
  }

  retain(ids: string[]): () => void {
    let added = false;
    for (const id of ids) {
      this.refs.set(id, (this.refs.get(id) || 0) + 1);
      if (!this.states.has(id)) added = true;
    }
    if (added) this.scheduleRetain();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      for (const id of ids) {
        const n = (this.refs.get(id) || 1) - 1;
        if (n <= 0) this.refs.delete(id);
        else this.refs.set(id, n);
      }
      this.scheduleRetain();
    };
  }

  private scheduleRetain(): void {
    clearTimeout(this.retainTimer);
    this.retainTimer = window.setTimeout(() => this.sendRetain(), 120);
  }

  private sendRetain(): void {
    this.ws?.send({ type: "ha_retain", ids: [...this.refs.keys()].sort() });
  }

  /** hass.connection.subscribeMessage(): resolves to an unsubscribe function.
   *  While disconnected the subscription is queued; onReconnect establishes it. */
  async subscribeMessage(cb: (ev: unknown) => void, msg: Record<string, unknown>): Promise<() => Promise<void>> {
    const local = this.nextReq++;
    const sub: Sub = { msg, cb, server: null, closed: false };
    this.subs.set(local, sub);
    if (this.ws?.isOpen()) {
      try {
        await this.establish(sub);
      } catch (e) {
        this.subs.delete(local);
        throw e;
      }
    }
    return async () => {
      sub.closed = true;
      this.subs.delete(local);
      if (sub.server !== null) {
        this.byServer.delete(sub.server);
        this.ws?.send({ type: "ha_unsubscribe", sub: sub.server });
      }
    };
  }

  private async establish(sub: Sub): Promise<void> {
    const req = this.nextReq++;
    const id = await new Promise<number>((resolve, reject) => {
      this.pendingSubs.set(req, { resolve, reject });
      this.ws!.send({ type: "ha_subscribe", req, msg: sub.msg });
      setTimeout(() => {
        if (this.pendingSubs.delete(req)) reject(new Error("subscribe timed out"));
      }, 15000);
    });
    if (sub.closed) {
      this.ws?.send({ type: "ha_unsubscribe", sub: id });
      return;
    }
    sub.server = id;
    this.byServer.set(id, sub);
    for (const ev of this.early.get(id) || []) sub.cb(ev);
    this.early.delete(id);
  }
}

export const haHub = new HaHub();
