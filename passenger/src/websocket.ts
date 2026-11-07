import type { ServerMessage, Source } from "./types";

const RECONNECT_MS = 3000;

/** Connects to the matching algorithm's WebSocket. Every message is the full list, so reconnecting needs no catch-up. */
export function websocketSource(url: string): Source {
  return (gates, onMessage) => {
    let socket: WebSocket | null = null;
    let stopped = false;

    const connect = () => {
      const target = new URL(url, location.href);
      if (gates) target.searchParams.set("gates", gates);
      socket = new WebSocket(target);
      socket.onmessage = (event) => {
        const msg = JSON.parse(event.data) as ServerMessage;
        if (msg.type === "assignments") onMessage(msg);
      };
      socket.onclose = () => {
        if (!stopped) setTimeout(connect, RECONNECT_MS);
      };
    };

    connect();
    return () => {
      stopped = true;
      socket?.close();
    };
  };
}
