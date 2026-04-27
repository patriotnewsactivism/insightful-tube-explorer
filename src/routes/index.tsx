import { createFileRoute, Link, redirect } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { ArrowRight, Mic, FileText, Users, Download, Sparkles, Clock } from "lucide-react";

export const Route = createFileRoute("/")({
  component: Landing,
});

function Landing() {
  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-6xl px-6">
        {/* Hero */}
        <section className="py-24 md:py-32 text-center">
          <div className="inline-flex items-center gap-2 rounded-full border border-border bg-surface/60 px-3 py-1 text-xs text-muted-foreground mb-8">
            <Sparkles className="h-3 w-3 text-primary" />
            Powered by Azure AI Speech + Lovable AI
          </div>
          <h1 className="font-display text-5xl md:text-7xl font-semibold leading-[1.05] tracking-tight">
            Read between<br />
            <span className="italic text-primary">the lines</span> of any video.
          </h1>
          <p className="mx-auto mt-6 max-w-xl text-lg text-muted-foreground">
            Paste a YouTube link. Get a diarized transcript, AI summary, sentiment, and deep notes. Lumen learns speakers over time so you always know who said what.
          </p>
          <div className="mt-10 flex items-center justify-center gap-3">
            <Button asChild size="lg" className="text-base">
              <Link to="/auth">
                Start analyzing <ArrowRight className="ml-1 h-4 w-4" />
              </Link>
            </Button>
          </div>
        </section>

        {/* Features */}
        <section className="pb-24 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[
            { icon: Mic, title: "Diarized transcript", body: "Azure Speech identifies each speaker, gently polished for readability without losing voice." },
            { icon: FileText, title: "Summary & sentiment", body: "Concise executive summary plus section-level sentiment you can skim." },
            { icon: Users, title: "Speaker memory", body: "Voice embeddings remember speakers across videos. Never re-label the same person twice." },
            { icon: Clock, title: "Production date", body: "AI infers the likely production date from title, description, and transcript clues." },
            { icon: Sparkles, title: "Expand on demand", body: "Generate deeper notes, study guides, or briefings from any section, anytime." },
            { icon: Download, title: "Export anywhere", body: "Markdown, PDF, or plain text. Autosaved so nothing is ever lost." },
          ].map((f) => (
            <div key={f.title} className="rounded-xl border border-border bg-surface/40 p-6 hover:bg-surface/70 transition-colors">
              <f.icon className="h-5 w-5 text-primary mb-4" />
              <h3 className="font-display text-lg font-semibold mb-1">{f.title}</h3>
              <p className="text-sm text-muted-foreground leading-relaxed">{f.body}</p>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}
