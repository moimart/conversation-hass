import { defineSetupPluginEntry } from "openclaw/plugin-sdk/channel-entry-contract";
import { halVoicePlugin } from "./channel.js";

export default defineSetupPluginEntry(halVoicePlugin);
