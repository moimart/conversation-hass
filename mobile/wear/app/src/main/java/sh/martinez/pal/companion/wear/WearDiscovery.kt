package sh.martinez.pal.companion.wear

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.Looper

/**
 * LAN autodiscovery for the watch: browse for the PAL ai-server's mDNS advert
 * (`_pal._tcp`, published by the pal-mdns container) via NsdManager and report
 * the first resolved `http://ip:port`. Best-effort — any failure (mDNS blocked,
 * nothing found before the timeout) simply never calls back and enrollment falls
 * back to the manually-typed URL. Acquires a Wi-Fi multicast lock for the browse
 * (needs CHANGE_WIFI_MULTICAST_STATE + ACCESS_WIFI_STATE in the manifest).
 */
object WearDiscovery {
    private const val SERVICE_TYPE = "_pal._tcp"

    fun discover(context: Context, timeoutMs: Long = 3000, onFound: (String) -> Unit) {
        val nsd = context.getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        val lock = wifi?.createMulticastLock("pal-mdns")?.apply {
            setReferenceCounted(true)
            runCatching { acquire() }
        }
        val main = Handler(Looper.getMainLooper())
        var settled = false
        lateinit var listener: NsdManager.DiscoveryListener

        fun cleanup() {
            if (settled) return
            settled = true
            runCatching { nsd.stopServiceDiscovery(listener) }
            if (lock?.isHeld == true) runCatching { lock.release() }
        }

        listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onServiceFound(info: NsdServiceInfo) {
                if (settled) return
                @Suppress("DEPRECATION")
                nsd.resolveService(info, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(s: NsdServiceInfo, errorCode: Int) {}
                    override fun onServiceResolved(s: NsdServiceInfo) {
                        @Suppress("DEPRECATION")
                        val host = s.host?.hostAddress ?: return
                        val url = "http://$host:${s.port}"
                        cleanup()
                        main.post { onFound(url) }
                    }
                })
            }
            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) { cleanup() }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }

        runCatching {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        }.onFailure { cleanup() }
        main.postDelayed({ cleanup() }, timeoutMs)
    }
}
