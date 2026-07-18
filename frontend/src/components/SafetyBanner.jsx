import { Shield } from "lucide-react";

export default function SafetyBanner() {
  return (
    <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 flex items-start gap-3">
      <Shield className="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
      <p className="text-xs text-amber-800 leading-relaxed">
        <strong>Educational tool only.</strong> GlucoPilot analyzes glucose data for informational purposes.
        It does not provide medical advice, insulin dosing recommendations, or control any medical device.
        Always consult your healthcare provider for treatment decisions.
      </p>
    </div>
  );
}