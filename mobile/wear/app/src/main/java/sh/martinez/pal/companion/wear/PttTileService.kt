package sh.martinez.pal.companion.wear

import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ColorBuilders.argb
import androidx.wear.protolayout.DimensionBuilders.expand
import androidx.wear.protolayout.LayoutElementBuilders.Box
import androidx.wear.protolayout.LayoutElementBuilders.Layout
import androidx.wear.protolayout.ModifiersBuilders.Background
import androidx.wear.protolayout.ModifiersBuilders.Clickable
import androidx.wear.protolayout.ModifiersBuilders.Corner
import androidx.wear.protolayout.ModifiersBuilders.Modifiers
import androidx.wear.protolayout.ResourceBuilders.Resources
import androidx.wear.protolayout.TimelineBuilders.Timeline
import androidx.wear.protolayout.material.Text
import androidx.wear.protolayout.material.Typography
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders.Tile
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

/**
 * A Tile (one swipe from the watch face): a big teal orb labelled "Talk to PAL"
 * that launches the app straight into listening (the EXTRA_START_PTT flag that
 * MainActivity arms on first composition).
 */
class PttTileService : TileService() {

    private val resVersion = "1"

    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest
    ): ListenableFuture<Tile> {
        val launch = Clickable.Builder()
            .setOnClick(
                ActionBuilders.LaunchAction.Builder()
                    .setAndroidActivity(
                        ActionBuilders.AndroidActivity.Builder()
                            .setPackageName(packageName)
                            .setClassName("sh.martinez.pal.companion.wear.MainActivity")
                            .addKeyToExtraMapping(
                                MainActivity.EXTRA_START_PTT,
                                ActionBuilders.AndroidStringExtra.Builder().setValue("1").build())
                            .build())
                    .build())
            .build()

        val orb = Box.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setModifiers(
                Modifiers.Builder()
                    .setClickable(launch)
                    .setBackground(
                        Background.Builder()
                            .setColor(argb(0xFF0E5C56.toInt()))
                            .setCorner(Corner.Builder().setRadius(
                                androidx.wear.protolayout.DimensionBuilders.dp(120f)).build())
                            .build())
                    .build())
            .addContent(
                Text.Builder(this, "Talk to PAL")
                    .setColor(argb(0xFFFFFFFF.toInt()))
                    .setTypography(Typography.TYPOGRAPHY_TITLE3)
                    .build())
            .build()

        val tile = Tile.Builder()
            .setResourcesVersion(resVersion)
            .setTileTimeline(Timeline.fromLayoutElement(
                Layout.Builder().setRoot(orb).build().root!!))
            .build()
        return Futures.immediateFuture(tile)
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest
    ): ListenableFuture<Resources> =
        Futures.immediateFuture(Resources.Builder().setVersion(resVersion).build())
}
