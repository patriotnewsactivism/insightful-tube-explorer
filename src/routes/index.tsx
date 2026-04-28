import { createFileRoute, Link } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { ArrowRight, Mic, FileText, Users, Download, Brain, Clock, Zap } from "lucide-react";

export const Route = createFileRoute("/")(
  { component: Landing }
);

function Landing() {
  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-6xl px-6">
        {/* Hero */}
        <section className="py-24 md:py-32 text-center">
          <div className="inline-flex items-center gap-2 rounded-full border border-red-200 bg-red-50/60 dark:border-red-900 dark:bg-red-950/40 px-4 py-1.5 text-xs font-medium text-red-700 dark:text-red-400 mb-8">
            <Zap className="h-3 w-3" />
            AI-Powered YouTube Intelligence
          </div>
          <h1 className="font-display text-5xl md:text-7xl font-bold leading-[1.05] tracking-tight">
            Every word.<br />
            Every speaker.<br />
            <span className="bg-gradient-to-r from-red-600 to-red-400 bg-clip-text text-transparent">Every insight.</span>
          </h1>
          <p className="mx-auto mt-6 max-w-xl text-lg text-muted-foreground leading-relaxed">
            Paste a YouTube link and get AI-powered transcripts with speaker
            identification, summaries, sentiment analysis, and production date
            detection. Built for researchers, journalists, and creators.
          </p>
          <div className="mt-10 flex items-center justify-center gap-3">
            <Button asChild size="lg" className="text-base bg-red-600 hover:bg-red-700 text-white shadow-lg shadow-red-600/25">
              <Link to="/auth">
                Start transcribing free <ArrowRight className="ml-1 h-4 w-4" />
              </Link>
            </Button>
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            3 free analyses · No credit card required
          </p>
        </section>

        {/* How it works */}
        <section className="pb-16">
          <h2 className="text-center font-display text-2xl font-semibold mb-10">
            Three steps. Instant insight.
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {[
              { step: "1", title: "Paste a link", body: "Drop any YouTube URL — single video or playlist. We handle the rest." },
              { step: "2", title: "AI processes", body: "Speaker diarization, transcription, summary, sentiment, and date analysis run in parallel." },
              { step: "3", title: "Export & cite", body: "Download polished transcripts as PDF, Word, or plain text. Ready for your research." },
            ].map((s) => (
              <div key={s.step} className="relative rounded-xl border border-border bg-surface/40 p-6">
                <div className="absolute -top-3 left-6 h-6 w-6 rounded-full bg-red-600 text-white text-xs font-bold grid place-items-center">{s.step}</div>
                <h3 className="font-display text-lg font-semibold mt-2 mb-1">{s.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{s.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* Features */}
        <section className="pb-24 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[
            { icon: Mic, title: "Speaker diarization", body: "Each speaker is identified and labeled. Polished for readability without losing authenticity." },
            { icon: FileText, title: "AI summary & sentiment", body: "Executive summary plus section-level sentiment you can skim in seconds." },
            { icon: Users, title: "Voice memory", body: "Speaker profiles persist across videos. Label once, recognize forever." },
            { icon: Clock, title: "Production date detection", body: "AI infers the likely recording date from title, description, and transcript context clues." },
            { icon: Brain, title: "Deep analysis", body: "Key claims, notable quotes, action items, and unanswered questions — all extracted automatically." },
            { icon: Download, title: "Export everything", body: "PDF, Word, Markdown, or plain text. Bulk export for entire research projects." },
          ].map((f) => (
            <div key={f.title} className="rounded-xl border border-border bg-surface/40 p-6 hover:bg-surface/70 transition-colors">
              <f.icon className="h-5 w-5 text-red-500 mb-4" />
              <h3 className="font-display text-lg font-semibold mb-1">{f.title}</h3>
              <p className="text-sm text-muted-foreground leading-relaxed">{f.body}</p>
            </div>
          ))}
        </section>

        {/* CTA */}
        <section className="pb-24 text-center">
          <div className="rounded-2xl border border-red-200 dark:border-red-900/50 bg-gradient-to-br from-red-50 to-orange-50 dark:from-red-950/30 dark:to-orange-950/20 p-12">
            <h2 className="font-display text-3xl font-bold mb-3">Ready to unlock your videos?</h2>
            <p className="text-muted-foreground mb-6 max-w-md mx-auto">
              Start with 3 free analyses. No credit card, no commitment.
            </p>
            <Button asChild size="lg" className="bg-red-600 hover:bg-red-700 text-white">
              <Link to="/auth">
                Get started free <ArrowRight className="ml-1 h-4 w-4" />
              </Link>
            </Button>
          </div>
        </section>

        {/* Footer */}
        <footer className="border-t border-border/60 py-8 text-center text-xs text-muted-foreground">
          &copy; {new Date().getFullYear()} TubeScribe. All rights reserved.
        </footer>
      </main>
    </div>
  );
}
