import { useCallback, useEffect, useRef, useState } from "react";

export const MAX_SECONDS = 60;
// The server decodes any of these; the browser picks the first it can record
// (Chrome/Firefox: WebM/Opus, Safari: MP4/AAC).
const TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

export function voiceSupported() {
  return typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof window.MediaRecorder !== "undefined";
}

function blobToAudioString(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const media = (blob.type || "audio/webm").split(";")[0];
      resolve(`${media}|${dataUrl.slice(dataUrl.indexOf(",") + 1)}`);
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

// Records one clip from the microphone and hands it over as "media/type|base64",
// the same encoding the label photo uses. status: idle | starting | recording |
// processing (onRecorded is awaited, so processing covers the upload too).
export default function useVoiceRecorder({ onRecorded, onError }) {
  const [status, setStatus] = useState("idle");
  const [seconds, setSeconds] = useState(0);
  const recorderRef = useRef(null);
  const streamRef = useRef(null);
  const timerRef = useRef(null);
  const cancelledRef = useRef(false);
  const callbacks = useRef({ onRecorded, onError });
  callbacks.current = { onRecorded, onError };

  const release = useCallback(() => {
    clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  const start = useCallback(async () => {
    if (!voiceSupported()) {
      callbacks.current.onError?.("This browser can't record audio. Type what you ate instead.");
      return false;
    }
    setStatus("starting");
    cancelledRef.current = false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      if (cancelledRef.current) {
        // cancelled (or unmounted) while the permission prompt was up
        stream.getTracks().forEach((t) => t.stop());
        return false;
      }
      streamRef.current = stream;
      const type = TYPES.find((t) => window.MediaRecorder.isTypeSupported?.(t)) || "";
      const recorder = new window.MediaRecorder(stream, type ? { mimeType: type, audioBitsPerSecond: 32000 } : undefined);
      const chunks = [];
      recorder.ondataavailable = (e) => { if (e.data?.size) chunks.push(e.data); };
      recorder.onstop = async () => {
        const mime = recorder.mimeType || type || "audio/webm";
        release();
        if (cancelledRef.current || !chunks.length) { setStatus("idle"); return; }
        setStatus("processing");
        try {
          await callbacks.current.onRecorded?.(await blobToAudioString(new Blob(chunks, { type: mime })));
        } finally {
          setStatus("idle");
        }
      };
      recorderRef.current = recorder;
      recorder.start();
      const startedAt = Date.now();
      setSeconds(0);
      timerRef.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startedAt) / 1000);
        setSeconds(elapsed);
        if (elapsed >= MAX_SECONDS && recorder.state === "recording") recorder.stop();
      }, 250);
      setStatus("recording");
      return true;
    } catch (err) {
      release();
      setStatus("idle");
      callbacks.current.onError?.(err?.name === "NotAllowedError"
        ? "Microphone access is blocked. Allow it in the browser, or type what you ate."
        : "Couldn't start the microphone. Type what you ate instead.");
      return false;
    }
  }, [release]);

  const stop = useCallback(() => {
    if (recorderRef.current?.state === "recording") recorderRef.current.stop();
  }, []);

  const cancel = useCallback(() => {
    cancelledRef.current = true;
    if (recorderRef.current?.state === "recording") recorderRef.current.stop();
    else { release(); setStatus("idle"); }
  }, [release]);

  useEffect(() => () => {
    cancelledRef.current = true;
    if (recorderRef.current?.state === "recording") recorderRef.current.stop();
    clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
  }, []);

  return { status, seconds, start, stop, cancel };
}
