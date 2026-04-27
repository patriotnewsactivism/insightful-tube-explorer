import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Loader2, ArrowLeft } from "lucide-react";
import { toast } from "sonner";
import { Link } from "@tanstack/react-router";

export const Route = createFileRoute("/analysis/$id")({
  component: AnalysisPage,
});

type Analysis = {
  id: string;
  youtube_url: string;
  youtube_id: string | null;
  title: string | null;
  channel: string | null;
  thumbnail_url: string | null;
  status: string;
  summary: string | null;
  polished_transcript: string | null;
  expanded_notes: string | null;
  likely_production_date: string | null;
  production_date_reasoning: string | null;
  sentiment: any;
  error_message: string | null;
};

function AnalysisPage() {
  const { id } = Route.useParams();
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [a, setA] = useState<Analysis | null>(null);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase.from("analyses").select("*").eq("id", id).single();
      if (error) toast.error(error.message);
      else setA(data as Analysis);
    })();

    const channel = supabase
      .channel(`analysis:${id}`)
      .on("postgres_changes", { event: "UPDATE", schema: "public", table: "analyses", filter: `id=eq.${id}` }, (payload) => {
        setA(payload.new as Analysis);
      })
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, [id, user]);

  if (!a) {
    return (
      <div className="min-h-screen">
        <SiteHeader />
        <div className="grid place-items-center py-32">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="mb-6 -ml-2">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4" /> Library</Link>
        </Button>

        <div className="mb-8">
          <h1 className="font-display text-3xl md:text-4xl font-semibold tracking-tight leading-tight">
            {a.title ?? "Untitled"}
          </h1>
          {a.channel && <p className="mt-2 text-muted-foreground">{a.channel}</p>}
        </div>

        {a.youtube_id && (
          <div className="aspect-video rounded-xl overflow-hidden border border-border bg-black mb-8 shadow-[var(--shadow-card)]">
            <iframe
              src={`https://www.youtube.com/embed/${a.youtube_id}`}
              title={a.title ?? "video"}
              className="h-full w-full"
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              allowFullScreen
            />
          </div>
        )}

        <div className="rounded-xl border border-border bg-surface/40 p-6">
          <h2 className="font-display text-lg font-semibold mb-2">Analysis</h2>
          <p className="text-sm text-muted-foreground">
            Status: <span className="text-foreground capitalize">{a.status}</span>
          </p>
          <p className="mt-4 text-sm text-muted-foreground">
            Transcription pipeline is set up but not yet wired. In the next step we'll connect it to your Azure worker to pull audio, transcribe with diarization, and fill in summary, sentiment, and notes.
          </p>
        </div>
      </main>
    </div>
  );
}
