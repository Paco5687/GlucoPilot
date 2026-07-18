import { useEffect, useState } from "react";
import { base44 } from "@/api/base44Client";
import { Loader2, CheckCircle2, XCircle } from "lucide-react";

export default function OuraCallback() {
  const [status, setStatus] = useState("processing"); // processing | success | error | denied
  const [message, setMessage] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const state = params.get("state");
    const error = params.get("error");

    if (error) {
      setStatus("denied");
      setMessage("You denied Oura access.");
      return;
    }

    if (!code) {
      setStatus("error");
      setMessage("No authorization code received.");
      return;
    }

    // Build the redirect_uri that matches what Oura has registered
    const redirectUri = window.location.origin + "/oura-callback";

    base44.functions.invoke("ouraAuth", { action: "exchange_code", code, state, redirect_uri: redirectUri })
      .then((res) => {
        if (res.data?.success) {
          setStatus("success");
          setMessage("Oura Ring connected successfully!");
          setTimeout(() => window.close(), 2000);
        } else {
          setStatus("error");
          setMessage(res.data?.error || "Failed to connect Oura.");
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
            <h2 className="text-lg font-semibold">Connecting Oura Ring...</h2>
            <p className="text-sm text-muted-foreground">Please wait while we complete the connection.</p>
          </>
        )}
        {status === "success" && (
          <>
            <CheckCircle2 className="w-10 h-10 text-green-500 mx-auto" />
            <h2 className="text-lg font-semibold">Connected!</h2>
            <p className="text-sm text-muted-foreground">{message}</p>
            <p className="text-xs text-muted-foreground">This window will close automatically.</p>
          </>
        )}
        {(status === "error" || status === "denied") && (
          <>
            <XCircle className="w-10 h-10 text-destructive mx-auto" />
            <h2 className="text-lg font-semibold">{status === "denied" ? "Access Denied" : "Connection Failed"}</h2>
            <p className="text-sm text-muted-foreground">{message}</p>
            <p className="text-xs text-muted-foreground">You can close this window and try again.</p>
          </>
        )}
      </div>
    </div>
  );
}