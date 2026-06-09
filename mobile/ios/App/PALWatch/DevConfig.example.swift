// Example shape of DevConfig.swift (which is GITIGNORED — it holds a real
// scoped pairing token). NOT compiled into the target; copy to DevConfig.swift
// and fill in. Dev-only scaffolding: replaced by Keychain storage + the
// phone-assisted WatchConnectivity enrollment in a later phase.
//
// Mint a watch token at home (LAN-only route), authorized by the phone's
// full token:
//   curl -X POST http://<pal>:8765/api/pair/derive \
//        -H "Authorization: Bearer <full-token>" \
//        -H "Content-Type: application/json" \
//        -d '{"scope": "watch", "device_name": "Apple Watch"}'
//
// import Foundation
//
// enum DevConfig {
//     static let serverBase = URL(string: "https://pal.example.com")!
//     static let watchToken = "<derived watch-scope token>"
// }
