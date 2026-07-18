import { useEffect, useState } from "react";
import { base44 } from "@/api/base44Client";
import { Loader2, CheckCircle2, XCircle } from "lucide-react";

export default function FitbitCallback() {
  const [status, setStatus] = useState("processing");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const error = params.get("error");

    if (error) {
      setStatus("denied");
      setMessage("You denied Fitbit access.");
      return;
    }
    if (!code) {
      setStatus("error");
      setMessage("No authorization code received.");
      return;
    }

    const redirectUri = window.location.origin + "/fitbit-callback";
    base44.functions.invoke("fitbit", { action: "exchange_code", code, redirect_uri: redirectUri })
      .then((res) => {
        if (res.data?.success) {
          setStatus("success");
          setMessage("Fitbit connected!");
          setTimeout(() => window.close(), 2000);
        } else {
          setStatus("error");
          setMessage(res.data?.error || "Failed to connect Fitbit.");
        }
      })
      .catch((err) => {
        setStatus("error");
        setMessage(err?.response?.data?.error || err.message || "Connection failed.");
      });
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-6">
      <div className="text-center space-y-4 max-w-sm">
        {status === "processing" && (
          <>
            <Loader2 className="w-10 h-10 animate-spin text-primary mx-auto" />
            <h2 className="text-lg font-semibold">Connecting Fitbit...</h2>
          </>
        )}
        {status === "success" && (
          <>
            <CheckCircle2 className="w-10 h-10 text-green-500 mx-auto" />
            <h2 className="text-lg font-semibold">Connected!</h2>
            <p className="text-sm text-muted-foreground">{message} This window will close automatically.</p>
          </>
        )}
        {(status === "error" || status === "denied") && (
          <>
            <XCircle className="w-10 h-10 text-destructive mx-auto" />
            <h2 className="text-lg font-semibold">{status === "denied" ? "Access Denied" : "Connection Failed"}</h2>
            <p className="text-sm text-muted-foreground">{message}</p>
          </>
        )}
      </div>
    </div>
  );
}
