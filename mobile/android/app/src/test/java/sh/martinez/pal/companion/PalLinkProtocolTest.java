package sh.martinez.pal.companion;

import static org.junit.Assert.*;

import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.Test;

public class PalLinkProtocolTest {

    private static byte[] b(int... v) {
        byte[] r = new byte[v.length];
        for (int i = 0; i < v.length; i++) r[i] = (byte) v[i];
        return r;
    }

    @Test
    public void helloEncodesVersionAndLittleEndianTime() {
        byte[] f = PalLinkProtocol.hello(0x0102030405060708L);
        assertArrayEquals(b(0x01, 0x00, 0x01, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01), f);
    }

    @Test
    public void simpleFrames() {
        assertArrayEquals(b(0x03, 5), PalLinkProtocol.opened(5));
        assertArrayEquals(b(0x05, 2, 1), PalLinkProtocol.close(2, PalLinkProtocol.R_REFUSED));
        assertArrayEquals(b(0x06, 3, 0x00, 0x10, 0x00, 0x00), PalLinkProtocol.credit(3, 4096));
        assertArrayEquals(b(0x06, 1, 0xFF, 0xFF, 0xFF, 0xFF), PalLinkProtocol.credit(1, 0xFFFFFFFFL));
        assertArrayEquals(b(0x04, 7, 'b', 'c'), PalLinkProtocol.data(7, b('a', 'b', 'c'), 1, 2));
    }

    @Test(expected = IllegalArgumentException.class)
    public void creditOutOfRange() {
        PalLinkProtocol.credit(1, 0x1_0000_0000L);
    }

    @Test
    public void decodeSplitsHeader() throws Exception {
        PalLinkProtocol.Frame f = PalLinkProtocol.decode(b(0x04, 9, 1, 2, 3));
        assertEquals(PalLinkProtocol.T_DATA, f.type);
        assertEquals(9, f.stream);
        assertArrayEquals(b(1, 2, 3), f.payload);
        PalLinkProtocol.Frame g = PalLinkProtocol.decode(b(0xFF, 0xFE));
        assertEquals(0xFF, g.type);
        assertEquals(0xFE, g.stream);
        assertEquals(0, g.payload.length);
    }

    @Test(expected = PalLinkProtocol.MalformedFrameException.class)
    public void decodeRejectsShort() throws Exception {
        PalLinkProtocol.decode(b(0x01));
    }

    @Test(expected = PalLinkProtocol.MalformedFrameException.class)
    public void decodeRejectsNull() throws Exception {
        PalLinkProtocol.decode(null);
    }

    @Test
    public void parseOpen() throws Exception {
        byte[] host = "pal.martinez.sh".getBytes(StandardCharsets.UTF_8);
        byte[] p = new byte[2 + host.length];
        p[0] = (byte) 0xBB; p[1] = 0x01;   // 443 LE
        System.arraycopy(host, 0, p, 2, host.length);
        PalLinkProtocol.Open o = PalLinkProtocol.parseOpen(p);
        assertEquals(443, o.port);
        assertEquals("pal.martinez.sh", o.host);
    }

    @Test
    public void parseOpenRejectsMalformed() {
        byte[][] bad = { b(), b(0xBB, 0x01), b(0xBB, 0x01, 0xC3), b(0xBB, 0x01, 'a', 0, 'b') };
        for (byte[] p : bad) {
            try {
                PalLinkProtocol.parseOpen(p);
                fail("accepted malformed OPEN");
            } catch (PalLinkProtocol.MalformedFrameException expected) { /* ok */ }
        }
    }

    @Test
    public void parseOthers() throws Exception {
        PalLinkProtocol.DeviceHello h = PalLinkProtocol.parseDeviceHello(b(1, 4));
        assertEquals(1, h.version);
        assertEquals(4, h.maxStreams);
        assertEquals(0x12345678L, PalLinkProtocol.parseCredit(b(0x78, 0x56, 0x34, 0x12)));
        assertEquals(0xFFFFFFFFL, PalLinkProtocol.parseCredit(b(0xFF, 0xFF, 0xFF, 0xFF)));
        assertEquals(1, PalLinkProtocol.parseMode(b(1)));
        assertEquals(PalLinkProtocol.R_NORMAL, PalLinkProtocol.parseCloseReason(b()));
        assertEquals(PalLinkProtocol.R_TIMEOUT, PalLinkProtocol.parseCloseReason(b(4)));
    }

    @Test
    public void parseShortPayloadsThrow() {
        try { PalLinkProtocol.parseDeviceHello(b(1)); fail(); } catch (PalLinkProtocol.MalformedFrameException e) { /* ok */ }
        try { PalLinkProtocol.parseCredit(b(1, 2, 3)); fail(); } catch (PalLinkProtocol.MalformedFrameException e) { /* ok */ }
        try { PalLinkProtocol.parseMode(b()); fail(); } catch (PalLinkProtocol.MalformedFrameException e) { /* ok */ }
    }

    @Test
    public void allowedOpen() {
        assertTrue(PalLinkProtocol.isAllowedOpen(443, "pal.martinez.sh", "pal.martinez.sh"));
        assertTrue(PalLinkProtocol.isAllowedOpen(443, "PAL.Martinez.SH", "pal.martinez.sh"));
        assertFalse(PalLinkProtocol.isAllowedOpen(80, "pal.martinez.sh", "pal.martinez.sh"));
        assertFalse(PalLinkProtocol.isAllowedOpen(443, "evil.example.com", "pal.martinez.sh"));
        assertFalse(PalLinkProtocol.isAllowedOpen(443, "pal.martinez.sh.evil.com", "pal.martinez.sh"));
        assertFalse(PalLinkProtocol.isAllowedOpen(443, "pal.martinez.sh", ""));
        assertFalse(PalLinkProtocol.isAllowedOpen(443, "pal.martinez.sh", null));
    }

    @Test
    public void frameSizes() {
        assertEquals(514, PalLinkProtocol.maxFrame(517));
        assertEquals(512, PalLinkProtocol.maxDataPayload(517));
        assertEquals(20, PalLinkProtocol.maxFrame(23));
        assertEquals(20, PalLinkProtocol.maxFrame(0));   // clamp to the default MTU
    }

    @Test
    public void receiveWindowRejectsOverrun() {
        PalLinkProtocol.Window w = new PalLinkProtocol.Window();
        assertTrue(w.onDataReceived(32768));
        assertFalse(w.onDataReceived(1));
    }

    @Test
    public void creditGrantedInChunksOfAtLeast4096() {
        PalLinkProtocol.Window w = new PalLinkProtocol.Window();
        assertTrue(w.onDataReceived(10000));
        assertEquals(0, w.onDrained(1000));
        assertEquals(0, w.onDrained(3000));
        assertEquals(4500, w.onDrained(500));        // 4500 ≥ 4096 → grant everything drained
        assertEquals(32768 - 10000 + 4500, w.recvAllowed());
        assertEquals(0, w.onDrained(4095));
        assertEquals(4096, w.onDrained(1));
    }

    @Test
    public void fullWindowNeverDeadlocks() {
        // Device fills the window, phone drains it all: the device must get room back.
        PalLinkProtocol.Window w = new PalLinkProtocol.Window();
        assertTrue(w.onDataReceived(32768));
        long granted = 0;
        for (int i = 0; i < 32768 / 512; i++) granted += w.onDrained(512);
        assertTrue(granted >= 32768 - 4095);
        assertTrue(w.recvAllowed() > 0);
    }

    @Test
    public void sendCreditBlocksUntilGranted() throws Exception {
        PalLinkProtocol.Window w = new PalLinkProtocol.Window();
        assertEquals(512, w.awaitSendCredit(512));
        w.consumeSend(32768);
        assertEquals(0, w.sendCredit());
        AtomicInteger got = new AtomicInteger(-99);
        Thread t = new Thread(() -> {
            try { got.set(w.awaitSendCredit(512)); } catch (InterruptedException ignored) { }
        });
        t.start();
        Thread.sleep(100);
        assertEquals(-99, got.get());     // still blocked
        w.addSendCredit(100);
        t.join(2000);
        assertEquals(100, got.get());
    }

    @Test
    public void closeWakesBlockedSender() throws Exception {
        PalLinkProtocol.Window w = new PalLinkProtocol.Window();
        w.consumeSend(32768);
        AtomicInteger got = new AtomicInteger(-99);
        Thread t = new Thread(() -> {
            try { got.set(w.awaitSendCredit(512)); } catch (InterruptedException ignored) { }
        });
        t.start();
        Thread.sleep(50);
        w.close();
        t.join(2000);
        assertEquals(-1, got.get());
        assertTrue(w.isClosed());
    }

    @Test
    public void hugeCreditIsClamped() {
        PalLinkProtocol.Window w = new PalLinkProtocol.Window();
        w.addSendCredit(0xFFFFFFFFL);
        w.addSendCredit(0xFFFFFFFFL);
        assertEquals(Integer.MAX_VALUE, w.sendCredit());
    }
}
