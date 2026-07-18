import SafetyBanner from "../components/SafetyBanner";
import DexcomSetup from "../components/connections/DexcomSetup";
import DexcomShareSetup from "../components/connections/DexcomShareSetup";
import NightscoutSetup from "../components/connections/NightscoutSetup";
import OuraSetup from "../components/connections/OuraSetup";
import FitbitSetup from "../components/connections/FitbitSetup";
import GoogleHealthSetup from "../components/connections/GoogleHealthSetup";
import TandemSetup from "../components/connections/TandemSetup";
import GlookoSetup from "../components/connections/GlookoSetup";
import { Plug, FileSpreadsheet } from "lucide-react";
import { Link } from "react-router-dom";

export default function Connections() {
  return (
    <div className="space-y-6">
      <SafetyBanner />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Connections</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Connect your data sources to sync glucose readings and treatments
          </p>
        </div>
        <Plug className="w-6 h-6 text-primary" />
      </div>

      {/* Dexcom Share — real-time feed */}
      <DexcomShareSetup />

      {/* Dexcom — official API (historical, ~1h delay) */}
      <DexcomSetup />

      {/* Tandem Source — pump treatment data */}
      <TandemSetup />

      {/* Glooko — failsafe treatment source (Tandem + Omnipod 5) */}
      <GlookoSetup />

      {/* Nightscout — per-user setup */}
      <NightscoutSetup />

      {/* Oura Ring */}
      <OuraSetup />

      {/* Fitbit */}
      <FitbitSetup />

      {/* Google Health (Fitbit's successor API) */}
      <GoogleHealthSetup />

      {/* CSV Import link */}
      <Link to="/import" className="block">
        <div className="bg-card rounded-xl border border-border p-5 hover:border-primary/30 transition-colors">
          <div className="flex items-start gap-4">
            <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center flex-shrink-0">
              <FileSpreadsheet className="w-6 h-6 text-primary" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <h3 className="font-semibold text-sm">CSV Import</h3>
                <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-green-100 text-green-700">
                  Available
                </span>
              </div>
              <p className="text-sm text-muted-foreground">
                Upload glucose data from CSV files exported from Glooko, Dexcom Clarity, or other CGM platforms.
              </p>
            </div>
          </div>
        </div>
      </Link>
    </div>
  );
}