import WidgetKit
import SwiftUI

/// A watch-face complication for PAL — a teal orb / mic glyph. Tapping it
/// launches the PAL watch app (default WidgetKit behaviour for an accessory
/// complication), so it's a one-tap quick-launch from the face.
struct PALEntry: TimelineEntry {
    let date: Date
}

struct PALProvider: TimelineProvider {
    func placeholder(in context: Context) -> PALEntry { PALEntry(date: .now) }
    func getSnapshot(in context: Context, completion: @escaping (PALEntry) -> Void) {
        completion(PALEntry(date: .now))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<PALEntry>) -> Void) {
        completion(Timeline(entries: [PALEntry(date: .now)], policy: .never))
    }
}

struct PALComplicationView: View {
    @Environment(\.widgetFamily) private var family
    var entry: PALEntry

    var body: some View {
        switch family {
        case .accessoryInline:
            Text("PAL")
        case .accessoryCorner:
            orb.widgetLabel("PAL")
        default: // accessoryCircular
            orb
        }
    }

    private var orb: some View {
        ZStack {
            AccessoryWidgetBackground()
            Image(systemName: "mic.fill")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(.teal)
        }
    }
}

@main
struct PALComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PALComplication", provider: PALProvider()) { entry in
            PALComplicationView(entry: entry)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("PAL")
        .description("Talk to PAL")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryInline])
    }
}
