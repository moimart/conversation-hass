package sh.martinez.pal.companion;

import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Locale;
import java.util.UUID;

/**
 * PAL Round ⇄ phone BLE link wire format + per-stream flow-control bookkeeping
 * (firmware/pal-round/BLE_LINK.md). Pure Java — no Android dependencies — so it's
 * unit-testable on the host.
 *
 * Frame: [type u8][stream u8][payload…], little-endian integers, stream 0 = control.
 * One frame = one ATT write / notification; the largest frame is MTU − 3 bytes.
 */
public final class PalLinkProtocol {

    private PalLinkProtocol() {}

    public static final UUID SERVICE_UUID = UUID.fromString("504c0001-0b1e-4f6c-9a3d-2b7e5a1c0000");
    /** device → phone, notify */
    public static final UUID TX_UUID = UUID.fromString("504c0002-0b1e-4f6c-9a3d-2b7e5a1c0000");
    /** phone → device, write without response */
    public static final UUID RX_UUID = UUID.fromString("504c0003-0b1e-4f6c-9a3d-2b7e5a1c0000");
    /** Client Characteristic Configuration descriptor. */
    public static final UUID CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    public static final int VERSION = 1;

    public static final int T_HELLO = 0x01;
    public static final int T_OPEN = 0x02;
    public static final int T_OPENED = 0x03;
    public static final int T_DATA = 0x04;
    public static final int T_CLOSE = 0x05;
    public static final int T_CREDIT = 0x06;
    public static final int T_MODE = 0x07;

    public static final int R_NORMAL = 0;
    public static final int R_REFUSED = 1;
    public static final int R_CONNECT_FAILED = 2;
    public static final int R_RESET = 3;
    public static final int R_TIMEOUT = 4;

    public static final int MODE_IDLE = 0;
    public static final int MODE_BUSY = 1;

    public static final int HEADER = 2;
    public static final int INITIAL_WINDOW = 32768;
    public static final int CREDIT_CHUNK = 4096;
    public static final int ALLOWED_PORT = 443;
    /** Default ATT MTU before (or without) an MTU exchange. */
    public static final int DEFAULT_MTU = 23;

    /** Thrown for frames/payloads that don't parse. Never escapes the service. */
    public static final class MalformedFrameException extends Exception {
        public MalformedFrameException(String msg) { super(msg); }
    }

    public static final class Frame {
        public final int type;
        public final int stream;
        public final byte[] payload;
        Frame(int type, int stream, byte[] payload) {
            this.type = type; this.stream = stream; this.payload = payload;
        }
    }

    public static final class DeviceHello {
        public final int version;
        public final int maxStreams;
        DeviceHello(int version, int maxStreams) { this.version = version; this.maxStreams = maxStreams; }
    }

    public static final class Open {
        public final int port;
        public final String host;
        Open(int port, String host) { this.port = port; this.host = host; }
    }

    // ---- sizes -------------------------------------------------------------

    /** Largest frame for a negotiated ATT MTU (MTU − 3). */
    public static int maxFrame(int mtu) {
        return Math.max(mtu, DEFAULT_MTU) - 3;
    }

    /** Largest DATA payload for a negotiated ATT MTU. */
    public static int maxDataPayload(int mtu) {
        return maxFrame(mtu) - HEADER;
    }

    // ---- encode (phone → device) ------------------------------------------

    public static byte[] hello(long unixTimeMs) {
        byte[] f = new byte[HEADER + 1 + 8];
        f[0] = (byte) T_HELLO;
        f[1] = 0;
        f[2] = (byte) VERSION;
        for (int i = 0; i < 8; i++) f[3 + i] = (byte) (unixTimeMs >>> (8 * i));
        return f;
    }

    public static byte[] opened(int stream) {
        return new byte[] { (byte) T_OPENED, (byte) stream };
    }

    public static byte[] data(int stream, byte[] buf, int off, int len) {
        byte[] f = new byte[HEADER + len];
        f[0] = (byte) T_DATA;
        f[1] = (byte) stream;
        System.arraycopy(buf, off, f, HEADER, len);
        return f;
    }

    public static byte[] close(int stream, int reason) {
        return new byte[] { (byte) T_CLOSE, (byte) stream, (byte) reason };
    }

    public static byte[] credit(int stream, long bytes) {
        if (bytes < 0 || bytes > 0xFFFFFFFFL) throw new IllegalArgumentException("credit out of range");
        byte[] f = new byte[HEADER + 4];
        f[0] = (byte) T_CREDIT;
        f[1] = (byte) stream;
        for (int i = 0; i < 4; i++) f[2 + i] = (byte) (bytes >>> (8 * i));
        return f;
    }

    // ---- decode (device → phone) ------------------------------------------

    public static Frame decode(byte[] raw) throws MalformedFrameException {
        if (raw == null || raw.length < HEADER) throw new MalformedFrameException("short frame");
        return new Frame(raw[0] & 0xFF, raw[1] & 0xFF, Arrays.copyOfRange(raw, HEADER, raw.length));
    }

    public static DeviceHello parseDeviceHello(byte[] p) throws MalformedFrameException {
        if (p.length < 2) throw new MalformedFrameException("short HELLO");
        return new DeviceHello(p[0] & 0xFF, p[1] & 0xFF);
    }

    public static Open parseOpen(byte[] p) throws MalformedFrameException {
        if (p.length < 3) throw new MalformedFrameException("short OPEN");
        int port = (p[0] & 0xFF) | ((p[1] & 0xFF) << 8);
        CharsetDecoder dec = StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT);
        String host;
        try {
            CharBuffer cb = dec.decode(ByteBuffer.wrap(p, 2, p.length - 2));
            host = cb.toString();
        } catch (CharacterCodingException e) {
            throw new MalformedFrameException("OPEN host not UTF-8");
        }
        if (host.isEmpty() || host.indexOf('\0') >= 0) throw new MalformedFrameException("bad OPEN host");
        return new Open(port, host);
    }

    public static long parseCredit(byte[] p) throws MalformedFrameException {
        if (p.length < 4) throw new MalformedFrameException("short CREDIT");
        long v = 0;
        for (int i = 0; i < 4; i++) v |= ((long) (p[i] & 0xFF)) << (8 * i);
        return v;
    }

    public static int parseMode(byte[] p) throws MalformedFrameException {
        if (p.length < 1) throw new MalformedFrameException("short MODE");
        return p[0] & 0xFF;
    }

    /** CLOSE reason is optional; absent = normal. */
    public static int parseCloseReason(byte[] p) {
        return p.length >= 1 ? (p[0] & 0xFF) : R_NORMAL;
    }

    /** The phone relays only to the paired gateway host on port 443. */
    public static boolean isAllowedOpen(int port, String host, String allowedHost) {
        if (port != ALLOWED_PORT || host == null || allowedHost == null) return false;
        String a = allowedHost.trim();
        if (a.isEmpty()) return false;
        return host.toLowerCase(Locale.ROOT).equals(a.toLowerCase(Locale.ROOT));
    }

    // ---- flow control ------------------------------------------------------

    /**
     * Credit windows for one stream, both directions. Thread-safe: the socket
     * reader thread blocks in {@link #awaitSendCredit} (no polling) while the GATT
     * thread adds credit; the writer thread reports drained bytes.
     */
    public static final class Window {
        /** phone → device: bytes we may still send. */
        private long sendCredit = INITIAL_WINDOW;
        /** device → phone: bytes the device may still send (granted − received). */
        private long recvAllowed = INITIAL_WINDOW;
        /** device → phone bytes written to the socket but not yet credited back. */
        private long drainedUncredited = 0;
        private boolean closed = false;

        /** Device granted more room. */
        public synchronized void addSendCredit(long n) {
            if (n <= 0) return;
            sendCredit = Math.min(sendCredit + n, Integer.MAX_VALUE);
            notifyAll();
        }

        /**
         * Block until there is send credit; return how many bytes (≤ max) may be
         * read+sent now, or -1 once the window is closed.
         */
        public synchronized int awaitSendCredit(int max) throws InterruptedException {
            while (!closed && sendCredit <= 0) wait();
            if (closed) return -1;
            return (int) Math.min(sendCredit, max);
        }

        public synchronized void consumeSend(int n) {
            sendCredit -= n;
        }

        /** DATA arrived from the device; false = it overran the granted window. */
        public synchronized boolean onDataReceived(int n) {
            if (n > recvAllowed) return false;
            recvAllowed -= n;
            return true;
        }

        /**
         * n device bytes were written to the socket. Returns the CREDIT to send
         * now (≥ CREDIT_CHUNK), or 0 to keep accumulating.
         */
        public synchronized long onDrained(int n) {
            drainedUncredited += n;
            if (drainedUncredited < CREDIT_CHUNK) return 0;
            long grant = drainedUncredited;
            drainedUncredited = 0;
            recvAllowed += grant;
            return grant;
        }

        public synchronized void close() {
            closed = true;
            notifyAll();
        }

        public synchronized boolean isClosed() { return closed; }
        public synchronized long sendCredit() { return sendCredit; }
        public synchronized long recvAllowed() { return recvAllowed; }
    }
}
