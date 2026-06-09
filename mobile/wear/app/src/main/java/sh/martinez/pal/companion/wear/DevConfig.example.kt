// Example shape of DevConfig.kt (which is GITIGNORED — it holds a real
// watch-scope pairing token). NOT compiled. Copy to DevConfig.kt and fill in.
// Dev-only scaffolding: replaced by on-watch enrollment (type the 6-digit
// pairing code → redeem scoped) in a later step.
//
// Mint a watch token at home (LAN-only route), authorized by the phone's
// full token:
//   curl -X POST http://<pal>:8765/api/pair/derive \
//        -H "Authorization: Bearer <full-token>" \
//        -H "Content-Type: application/json" \
//        -d '{"scope":"watch","device_name":"Pixel Watch 3"}'
//
// package sh.martinez.pal.companion.wear
//
// object DevConfig {
//     const val SERVER_BASE = "https://pal.example.com"
//     const val WATCH_TOKEN = "<derived watch-scope token>"
// }
