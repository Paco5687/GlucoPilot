import { Outlet, Link, useLocation } from "react-router-dom";
import {
  LayoutDashboard, LineChart, Brain, GitCompare, MessageSquare, Plug, Menu, X, Shield,
  Heart, LogOut, Settings, Lightbulb, FolderHeart, FileText, Eye, Watch, Sparkles, Syringe, MessageCircleHeart, NotebookPen,
} from "lucide-react";
import { Bug } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/AuthContext";
import { pushTrail } from "@/lib/navTrail";
import BugReportModal from "@/components/BugReportModal";

// Grouped navigation. adminOnly items are hidden from read-only provider sessions.
const navGroups = [
  {
    label: null,
    items: [
      { path: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
      { path: "/explorer", label: "Explorer", icon: LineChart },
    ],
  },
  {
    label: "Analysis",
    items: [
      { path: "/patterns", label: "Patterns", icon: Brain },
      { path: "/insights", label: "Insights", icon: Lightbulb },
      { path: "/insulin", label: "Insulin", icon: Syringe },
      { path: "/compare", label: "Compare", icon: GitCompare },
      { path: "/analyst", label: "AI Analyst", icon: MessageSquare, adminOnly: true },
      { path: "/companion", label: "Companion", icon: MessageCircleHeart, adminOnly: true },
    ],
  },
  {
    label: "Health",
    items: [
      { path: "/overview", label: "Overview", icon: Sparkles },
      { path: "/symptoms", label: "Symptoms", icon: NotebookPen, adminOnly: true },
      { path: "/period", label: "Cycle", icon: Heart },
      { path: "/wearables", label: "Wearables", icon: Watch },
      { path: "/records", label: "Records", icon: FolderHeart },
      { path: "/report", label: "Visit Report", icon: FileText },
    ],
  },
  {
    label: "Setup",
    items: [
      { path: "/connections", label: "Connections", icon: Plug, adminOnly: true },
      { path: "/settings", label: "Settings", icon: Settings, adminOnly: true },
    ],
  },
];

function NavLink({ item, active, onClick }) {
  const Icon = item.icon;
  return (
    <Link
      to={item.path}
      onClick={onClick}
      className={cn(
        "flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors",
        active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground hover:bg-accent"
      )}
    >
      <Icon className="w-4 h-4 flex-shrink-0" />
      {item.label}
    </Link>
  );
}

export default function Layout() {
  const location = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const { logout, isAdmin, isProvider, user } = useAuth();
  const isDemo = user?.demo;
  const [bugOpen, setBugOpen] = useState(false);
  const close = () => setMobileOpen(false);

  useEffect(() => {
    pushTrail(location.pathname);
  }, [location.pathname]);

  const groups = navGroups
    .map((g) => ({ ...g, items: g.items.filter((i) => !i.adminOnly || isAdmin) }))
    .filter((g) => g.items.length);

  const Brand = (
    <Link to="/dashboard" onClick={close} className="flex items-center gap-2.5">
      <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
        <span className="text-primary-foreground font-mono font-bold text-sm">GP</span>
      </div>
      <span className="font-semibold tracking-tight">GlucoPilot</span>
    </Link>
  );

  return (
    <div className="min-h-screen bg-background">
      {/* Sidebar */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-60 bg-card border-r border-border flex flex-col transition-transform duration-200 print:hidden",
          "lg:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        <div className="h-14 flex items-center justify-between px-4 border-b border-border">
          {Brand}
          <button className="lg:hidden p-1.5 rounded-lg hover:bg-accent" onClick={close}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-1">
          {groups.map((group, gi) => (
            <div key={gi} className={gi > 0 ? "pt-4" : ""}>
              {group.label && (
                <p className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                  {group.label}
                </p>
              )}
              <div className="space-y-0.5">
                {group.items.map((item) => (
                  <NavLink key={item.path} item={item} active={location.pathname === item.path} onClick={close} />
                ))}
              </div>
            </div>
          ))}
        </nav>

        {/* Footer: account */}
        <div className="border-t border-border p-3 space-y-1">
          <div className="flex items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground">
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Shield className="w-3.5 h-3.5 cursor-help flex-shrink-0" />
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs text-xs">
                  Educational tool only. GlucoPilot analyzes health data for informational purposes. It does not
                  provide medical advice, insulin dosing, or control any medical device. Always consult your healthcare
                  provider for treatment decisions.
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <span className="truncate">{user?.full_name || "Signed in"}</span>
          </div>
          <button
            onClick={() => { setBugOpen(true); close(); }}
            className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <Bug className="w-4 h-4" /> Report a bug
          </button>
          <button
            onClick={logout}
            className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <LogOut className="w-4 h-4" /> Sign out
          </button>
        </div>
      </aside>

      <BugReportModal open={bugOpen} onClose={() => setBugOpen(false)} />

      {/* Mobile backdrop */}
      {mobileOpen && <div className="fixed inset-0 z-40 bg-black/40 lg:hidden" onClick={close} />}

      {/* Content column */}
      <div className="lg:pl-60 print:pl-0">
        {/* Mobile top bar */}
        <header className="lg:hidden sticky top-0 z-30 h-14 flex items-center gap-3 px-4 bg-card/80 backdrop-blur-xl border-b border-border print:hidden">
          <button className="p-1.5 rounded-lg hover:bg-accent" onClick={() => setMobileOpen(true)}>
            <Menu className="w-5 h-5" />
          </button>
          {Brand}
        </header>

        {isDemo && (
          <div className="bg-amber-50 border-b border-amber-200 text-amber-800 text-xs font-medium px-4 py-2 text-center print:hidden">
            🧪 Demo — all data below is synthetic sample data, not real health information.
          </div>
        )}
        {isProvider && (
          <div className="bg-sky-50 border-b border-sky-200 text-sky-800 text-xs font-medium px-4 py-2 text-center flex items-center justify-center gap-1.5 print:hidden">
            <Eye className="w-3.5 h-3.5" /> Read-only provider view — you can view and print reports, but not change data or settings.
          </div>
        )}

        <main className="max-w-7xl mx-auto px-4 lg:px-8 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
