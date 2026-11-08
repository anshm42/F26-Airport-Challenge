import type { ServerMessage, Source } from "./types";

export const RECONNECT_MS = 3000;

/** Connects to the matching algorithm's WebSocket. Every message is the full list, so reconnecting needs no catch-up. The server doesn't filter, so `gates` isn't sent. */
export function websocketSource(url: string): Source {
  return (_gates, onMessage, onConnection) => {
    let socket: WebSocket | null = null;
    let stopped = false;

    const connect = () => {
      socket = new WebSocket(new URL(url, location.href));
      socket.onmessage = (event) => {
        const msg = JSON.parse(event.data) as ServerMessage;
        if (msg.type === "assignments" || msg.type === "heartbeat") onMessage(msg);
      };
      socket.onopen = () => onConnection?.("open");
      // A refused or dropped connection: report it and keep retrying, so the view comes back when the matcher does.
      socket.onclose = () => {
        if (stopped) return;
        onConnection?.("closed");
        setTimeout(connect, RECONNECT_MS);
      };
    };

    connect();
    return () => {
      stopped = true;
      socket?.close();
    };
  };
}
