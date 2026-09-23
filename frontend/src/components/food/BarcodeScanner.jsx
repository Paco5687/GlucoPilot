import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { X, Loader2 } from "lucide-react";

// Camera barcode reader. Uses the browser's native BarcodeDetector where it
// exists (Chrome/Android) and falls back to ZXing elsewhere (Safari/iOS).
// Nothing leaves the device from here; the decoded digits go to onDetected.
export default function BarcodeScanner({ onDetected, onClose }) {
  const videoRef = useRef(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(true);

  useEffect(() => {
    let stream = null;
    let stopped = false;
    let zxingControls = null;
    let timer = null;

    async function start() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
        if (stopped) { stream.getTracks().forEach((t) => t.stop()); return; }
        const video = videoRef.current;
        video.srcObject = stream;
        await video.play();
        setStarting(false);
        if ("BarcodeDetector" in window) {
          const detector = new window.BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"] });
          const tick = async () => {
            if (stopped) return;
            try {
              const codes = await detector.detect(video);
              const hit = codes.find((c) => c.rawValue);
              if (hit) { onDetected(hit.rawValue); return; }
            } catch { /* frame not ready */ }
            timer = setTimeout(tick, 200);
          };
          tick();
        } else {
          const { BrowserMultiFormatReader } = await import("@zxing/browser");
          const reader = new BrowserMultiFormatReader();
          zxingControls = await reader.decodeFromVideoElement(video, (result) => {
            if (result && !stopped) onDetected(result.getText());
          });
        }
      } catch (err) {
        setError(err?.name === "NotAllowedError" ? "Camera permission was denied." : "Couldn't start the camera on this device.");
        setStarting(false);
      }
    }
    start();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (zxingControls) zxingControls.stop();
      if (stream) stream.getTracks().forEach((t) => t.stop());
    };
  }, [onDetected]);

  return (
    <div className="fixed inset-0 z-50 bg-black/90 flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-md space-y-3">
        <div className="relative rounded-xl overflow-hidden bg-black aspect-[3/4]">
          <video ref={videoRef} className="w-full h-full object-cover" muted playsInline />
          <div className="absolute inset-x-8 top-1/2 -translate-y-1/2 h-28 border-2 border-white/80 rounded-lg pointer-events-none" />
          {starting && !error && <div className="absolute inset-0 flex items-center justify-center text-white"><Loader2 className="w-6 h-6 animate-spin" /></div>}
        </div>
        {error ? <p className="text-sm text-white text-center">{error}</p> : <p className="text-xs text-white/80 text-center">Point the camera at the barcode</p>}
        <Button variant="secondary" className="w-full" onClick={onClose}><X className="w-4 h-4 mr-1" /> Close</Button>
      </div>
    </div>
  );
}
