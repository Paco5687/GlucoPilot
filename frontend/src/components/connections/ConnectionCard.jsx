import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { CheckCircle, XCircle, Clock, ExternalLink } from "lucide-react";

const STATUS_CONFIG = {
  connected: {
    icon: CheckCircle,
    label: "Connected",
    color: "text-green-600",
    bg: "bg-green-100",
  },
  disconnected: {
    icon: XCircle,
    label: "Not Connected",
    color: "text-muted-foreground",
    bg: "bg-muted",
  },
  planned: {
    icon: Clock,
    label: "Coming Soon",
    color: "text-amber-600",
    bg: "bg-amber-100",
  },
};

export default function ConnectionCard({ name, description, status, lastSync, icon: IconComponent, onConnect, onDisconnect }) {
  const statusCfg = STATUS_CONFIG[status] || STATUS_CONFIG.disconnected;
  const StatusIcon = statusCfg.icon;

  return (
    <div className="bg-card rounded-xl border border-border p-5">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center flex-shrink-0">
          {IconComponent}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">{name}</h3>
            <div className={cn("flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium", statusCfg.bg, statusCfg.color)}>
              <StatusIcon className="w-3 h-3" />
              {statusCfg.label}
            </div>
          </div>
          <p className="text-sm text-muted-foreground leading-relaxed mb-3">{description}</p>
          {lastSync && (
            <p className="text-xs text-muted-foreground mb-3">
              Last synced: {lastSync}
            </p>
          )}
          <div className="flex items-center gap-2">
            {status === "connected" ? (
              <>
                <Button size="sm" variant="outline" onClick={onDisconnect}>
                  Disconnect
                </Button>
                <Button size="sm" variant="ghost" className="gap-1 text-xs">
                  <ExternalLink className="w-3 h-3" /> Settings
                </Button>
              </>
            ) : status === "disconnected" ? (
              <Button size="sm" onClick={onConnect}>
                Connect
              </Button>
            ) : (
              <Button size="sm" variant="outline" disabled>
                Coming Soon
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}