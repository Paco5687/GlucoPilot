import { base44 } from "@/api/base44Client";
import { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import {
  Activity, Code2, EyeOff, FileText, FlaskConical, Github, HardDrive, Heart, KeyRound, MessageSquare, Syringe, Terminal,
} from "lucide-react";
import { Button } from "@/components/ui/button";

const REPO_URL = "https://github.com/Paco5687/GlucoPilot";

const INSTALL_COMMAND = `git clone ${REPO_URL} glucopilot && cd glucopilot
./install.sh`;

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

function RepoLink({ path = "", children }) {
  return (
    <a
      href={`${REPO_URL}${path}`}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary hover:underline"
    >
      {children}
    </a>
  );
}

const signIn = () => base44.auth.redirectToLogin("/dashboard");

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
          <div className="flex items-center gap-4">
            <button
              type="button"
              onClick={signIn}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors"
            >
              Sign in
            </button>
            <Button asChild variant="outline" size="sm">
              <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
                <Github className="w-4 h-4 mr-1.5" /> GitHub
              </a>
            </Button>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="max-w-6xl mx-auto px-4 py-20 md:py-32">
        <div className="max-w-3xl mx-auto text-center">
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-primary/10 text-primary text-xs font-medium mb-6">
            <Code2 className="w-3.5 h-3.5" />
            Open source · Self-hosted · MIT license
          </div>
          <h1 className="text-4xl md:text-6xl font-bold text-foreground tracking-tight leading-tight mb-6">
            Your diabetes data,
            <br />
            <span className="text-primary">on your own server.</span>
          </h1>
          <p className="text-lg text-muted-foreground max-w-2xl mx-auto mb-4 leading-relaxed">
            GlucoPilot is a free, open-source health app for Type 1 diabetes that you install and run yourself.
            It brings CGM, insulin pump, wearable, cycle, and lab data into one private dashboard, with a
            Companion chat that answers from your own readings.
          </p>
          <p className="text-sm text-muted-foreground max-w-xl mx-auto mb-10">
            There is no hosted service and no sign-up. Each install is its own private server.
          </p>
          <div className="flex flex-col sm:flex-row gap-3 justify-center">
            <Button asChild size="lg" className="text-base px-8">
              <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
                <Github className="w-4 h-4 mr-2" /> View on GitHub
              </a>
            </Button>
            <Button size="lg" variant="outline" className="text-base px-8" onClick={() => document.getElementById("install")?.scrollIntoView({ behavior: "smooth" })}>
              <Terminal className="w-4 h-4 mr-2" /> Install it
            </Button>
          </div>
        </div>
      </section>

      {/* Self-hosted */}
      <section id="self-hosted" className="max-w-6xl mx-auto px-4 pb-20">
        <div className="text-center mb-12">
          <h2 className="text-2xl md:text-3xl font-bold text-foreground mb-3">You run it. You keep the data.</h2>
          <p className="text-muted-foreground max-w-xl mx-auto">
            GlucoPilot is software you host, not an account you sign up for.
          </p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
          <FeatureCard
            icon={HardDrive}
            title="Runs on your hardware"
            description="One Docker container on a home server, a VPS, or a laptop. All data lives in a single volume you control."
            color="bg-primary/10 text-primary"
          />
          <FeatureCard
            icon={EyeOff}
            title="No telemetry"
            description="No analytics and no phone-home. The app talks only to the services you connect."
            color="bg-blue-500/10 text-blue-600"
          />
          <FeatureCard
            icon={KeyRound}
            title="Your keys, your model"
            description="Use your own Anthropic or OpenAI key, or a local model so no health data leaves the machine."
            color="bg-amber-500/10 text-amber-600"
          />
          <FeatureCard
            icon={Code2}
            title="MIT licensed"
            description="Read the code, change it, and run your own fork. Issues and pull requests go through GitHub."
            color="bg-green-500/10 text-green-600"
          />
        </div>
      </section>

      {/* Features */}
      <section id="features" className="max-w-6xl mx-auto px-4 pb-20">
        <div className="text-center mb-12">
          <h2 className="text-2xl md:text-3xl font-bold text-foreground mb-3">What it does</h2>
          <p className="text-muted-foreground max-w-2xl mx-auto">
            Connects to Dexcom, Nightscout, Glooko (Omnipod 5), Tandem Source, Oura, and Fitbit, or imports CSV.
          </p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          <FeatureCard
            icon={MessageSquare}
            title="Companion"
            description="A health chat grounded in your glucose, labs, cycle, sleep, and symptoms, with the evidence behind each answer. It does not diagnose or suggest doses."
            color="bg-purple-500/10 text-purple-600"
          />
          <FeatureCard
            icon={Activity}
            title="Glucose dashboard and explorer"
            description="Live readings, time in range, GMI, CV, and AGP, plus a zoomable chart with insulin and basal overlaid."
            color="bg-primary/10 text-primary"
          />
          <FeatureCard
            icon={Syringe}
            title="Insulin"
            description="Daily totals with basal/bolus split, correction response, and time in range by pump mode."
            color="bg-amber-500/10 text-amber-600"
          />
          <FeatureCard
            icon={Heart}
            title="Cycle"
            description="Menstrual phases inferred from Oura nightly temperature and lined up against glucose and insulin."
            color="bg-red-500/10 text-red-500"
          />
          <FeatureCard
            icon={FlaskConical}
            title="Records"
            description="Upload lab reports as PDF or photo. Values are extracted into per-analyte trend charts."
            color="bg-green-500/10 text-green-600"
          />
          <FeatureCard
            icon={FileText}
            title="Visit Report and provider access"
            description="A printable 90-day clinical summary, and a read-only login you can give your doctor."
            color="bg-blue-500/10 text-blue-600"
          />
        </div>
      </section>

      {/* Install */}
      <section id="install" className="bg-primary/5 border-t border-border">
        <div className="max-w-3xl mx-auto px-4 py-16">
          <h2 className="text-2xl font-bold text-foreground mb-3 text-center">Get it running</h2>
          <p className="text-muted-foreground mb-6 text-center">
            You need <a href="https://docs.docker.com/get-docker/" target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">Docker</a>.
            Clone the repository and run the installer:
          </p>
          <pre className="bg-card border border-border rounded-xl p-4 text-sm font-mono text-foreground overflow-x-auto mb-6">
            <code>{INSTALL_COMMAND}</code>
          </pre>
          <p className="text-sm text-muted-foreground mb-8 text-center leading-relaxed">
            The installer writes your config, asks for a port, timezone, and optional AI key, starts the app,
            and prints the link. Open it and create your login.
          </p>
          <div className="flex flex-col sm:flex-row flex-wrap items-center gap-x-6 gap-y-2 justify-center text-sm">
            <RepoLink path="#quick-start">Setup guide</RepoLink>
            <RepoLink path="/blob/main/docs/DEPLOY.md">Server deploy with HTTPS</RepoLink>
            <RepoLink path="/blob/main/docs/LOCAL_MODELS.md">Fully local AI</RepoLink>
            <RepoLink path="#try-the-demo">Demo with synthetic data</RepoLink>
          </div>
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
            <a href={REPO_URL} target="_blank" rel="noopener noreferrer" className="hover:text-foreground transition-colors">GitHub</a>
            <Link to="/privacy" className="hover:text-foreground transition-colors">Privacy Policy</Link>
            <Link to="/terms" className="hover:text-foreground transition-colors">Terms of Service</Link>
          </div>
        </div>
        <p className="max-w-6xl mx-auto px-4 mt-6 text-center text-xs text-muted-foreground/70">
          This server is a private GlucoPilot install. Accounts are by invitation from its owner.
        </p>
      </footer>
    </div>
  );
}
