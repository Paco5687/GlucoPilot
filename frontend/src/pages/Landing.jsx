import { base44 } from "@/api/base44Client";
import { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Activity, TrendingUp, Brain, Heart, Shield, ArrowRight, BarChart3, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";

function FeatureCard({ icon: Icon, title, description, color }) {
  return (
    <div className="bg-card rounded-2xl border border-border p-6 hover:shadow-lg transition-all hover:-translate-y-1">
      <div className={`w-10 h-10 rounded-xl flex items-center justify-center mb-4 ${color}`}>
        <Icon className="w-5 h-5" />
      </div>
      <h3 className="font-semibold text-foreground mb-2">{title}</h3>
      <p className="text-sm text-muted-foreground leading-relaxed">{description}</p>
    </div>
  );
}

export default function Landing() {
  const navigate = useNavigate();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    base44.auth.isAuthenticated().then((authed) => {
      if (authed) navigate("/dashboard", { replace: true });
      else setChecking(false);
    }).catch(() => setChecking(false));
  }, [navigate]);

  if (checking) {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-background">
        <div className="w-8 h-8 border-4 border-slate-200 border-t-primary rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Nav */}
      <header className="sticky top-0 z-50 bg-background/80 backdrop-blur-xl border-b border-border">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
              <span className="text-primary-foreground font-mono font-bold text-sm">GP</span>
            </div>
            <span className="font-semibold text-foreground tracking-tight">GlucoPilot</span>
          </div>
          <Button onClick={() => base44.auth.redirectToLogin("/dashboard")}>
            Sign In <ArrowRight className="w-4 h-4 ml-1" />
          </Button>
        </div>
      </header>

      {/* Hero */}
      <section className="max-w-6xl mx-auto px-4 py-20 md:py-32">
        <div className="max-w-3xl mx-auto text-center">
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-primary/10 text-primary text-xs font-medium mb-6">
            <Activity className="w-3.5 h-3.5" />
            Intelligent Glucose Analytics
          </div>
          <h1 className="text-4xl md:text-6xl font-bold text-foreground tracking-tight leading-tight mb-6">
            Understand your glucose,
            <br />
            <span className="text-primary">own your health.</span>
          </h1>
          <p className="text-lg text-muted-foreground max-w-2xl mx-auto mb-10 leading-relaxed">
            GlucoPilot connects to your CGM data and gives you AI-powered pattern analysis,
            cycle tracking, and deep insights — all in one beautiful dashboard.
          </p>
          <div className="flex flex-col sm:flex-row gap-3 justify-center">
            <Button size="lg" className="text-base px-8" onClick={() => base44.auth.redirectToLogin("/dashboard")}>
              Get Started <ArrowRight className="w-4 h-4 ml-2" />
            </Button>
            <Button size="lg" variant="outline" className="text-base px-8" onClick={() => document.getElementById("features")?.scrollIntoView({ behavior: "smooth" })}>
              Learn More
            </Button>
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="max-w-6xl mx-auto px-4 pb-20">
        <div className="text-center mb-12">
          <h2 className="text-2xl md:text-3xl font-bold text-foreground mb-3">Everything you need to manage diabetes smarter</h2>
          <p className="text-muted-foreground max-w-xl mx-auto">Built for people with diabetes who want deeper insight into their data.</p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          <FeatureCard
            icon={TrendingUp}
            title="Real-Time CGM Dashboard"
            description="Live glucose readings with trend arrows, time-in-range metrics, and interactive charts — synced from Nightscout."
            color="bg-primary/10 text-primary"
          />
          <FeatureCard
            icon={Brain}
            title="AI Pattern Detection"
            description="Automatically detects recurring highs, post-meal spikes, dawn phenomenon, and more using intelligent analysis."
            color="bg-purple-500/10 text-purple-600"
          />
          <FeatureCard
            icon={BarChart3}
            title="Period Comparisons"
            description="Compare weeks, weekdays vs weekends, or any custom date ranges to spot trends and track progress."
            color="bg-amber-500/10 text-amber-600"
          />
          <FeatureCard
            icon={Heart}
            title="Cycle Tracking"
            description="Log your menstrual cycle and see phase overlays on your glucose chart to understand hormonal impacts."
            color="bg-red-500/10 text-red-500"
          />
          <FeatureCard
            icon={Zap}
            title="Nightscout Integration"
            description="Seamlessly syncs with your Nightscout instance to pull glucose readings, treatments, and profile data."
            color="bg-green-500/10 text-green-600"
          />
          <FeatureCard
            icon={Shield}
            title="Private & Secure"
            description="Your health data stays yours. No third-party sharing, no ads — just a personal analytics tool for your data."
            color="bg-blue-500/10 text-blue-600"
          />
        </div>
      </section>

      {/* CTA */}
      <section className="bg-primary/5 border-t border-border">
        <div className="max-w-6xl mx-auto px-4 py-16 text-center">
          <h2 className="text-2xl font-bold text-foreground mb-3">Ready to take control?</h2>
          <p className="text-muted-foreground mb-8 max-w-lg mx-auto">
            Sign in to connect your CGM data and start getting smarter insights today.
          </p>
          <Button size="lg" className="text-base px-8" onClick={() => base44.auth.redirectToLogin("/dashboard")}>
            Sign In to GlucoPilot <ArrowRight className="w-4 h-4 ml-2" />
          </Button>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-border py-8">
        <div className="max-w-6xl mx-auto px-4 flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-2">
            <div className="w-5 h-5 rounded bg-primary flex items-center justify-center">
              <span className="text-primary-foreground font-mono font-bold text-[8px]">GP</span>
            </div>
            GlucoPilot
          </div>
          <p>Educational tool only. Not medical advice. Always consult your healthcare provider.</p>
          <div className="flex items-center gap-4">
            <Link to="/privacy" className="hover:text-foreground transition-colors">Privacy Policy</Link>
            <Link to="/terms" className="hover:text-foreground transition-colors">Terms of Service</Link>
          </div>
        </div>
      </footer>
    </div>
  );
}