import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { extractYouTubeId, fetchYouTubeOEmbed } from "@/lib/youtube";
import { toast } from "sonner";
import { Plus, Loader2, Play } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

export const Route = createFileRoute("/dashboard")({
  component: Dashboard,
});

type Analysis = {
  id: string;
  youtube_url: string;
  title: string | null;
  channel: string | null;
  thumbnail_url: string | null;
  status: string;
  created_at: string;
};

function Dashboard() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [creating, setCreating] = useState(false);
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [listLoading, setListLoading] = useState(true);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("analyses")
        .select("id, youtube_url, title, channel, thumbnail_url, status, created_at")
        .order("created_at", { ascending: false });
      if (error) toast.error(error.message);
      else setAnalyses(data as Analysis[]);
      setListLoading(false);
    })();
  }, [user]);

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!user) return;
    const ytId = extractYouTubeId(url);
    if (!ytId) return toast.error("That doesn't look like a YouTube URL");
    setCreating(true);
    try {
      const oembed = await fetchYouTubeOEmbed(url).catch(() => null);
      const { data, error } = await supabase
        .from("analyses")
        .insert({
          user_id: user.id,
          youtube_url: url,
          youtube_id: ytId,
          title: oembed?.title ?? null,
          channel: oembed?.author_name ?? null,
          thumbnail_url: oembed?.thumbnail_url ?? `https://i.ytimg.com/vi/${ytId}/hqdefault.jpg`,
          status: "pending",
        })
        .select("id")
        .single();
      if (error) throw error;
      navigate({ to: "/analysis/$id", params: { id: data.id } });
    } catch (err: any) {
      toast.error(err.message ?? "Could not create analysis");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-5xl px-6 py-12">
        <div className="mb-10">
          <h1 className="font-display text-4xl font-semibold tracking-tight">Your library</h1>
          <p className="mt-2 text-muted-foreground">Paste a YouTube URL to analyze it. All work is autosaved.</p>
        </div>

        <form onSubmit={onCreate} className="flex gap-2 mb-12">
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.youtube.com/watch?v=..."
            className="h-12 text-base"
          />
          <Button type="submit" size="lg" disabled={creating || !url}>
            {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            New analysis
          </Button>
        </form>

        {listLoading ? (
          <div className="grid place-items-center py-20">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : analyses.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/30 p-12 text-center">
            <p className="text-muted-foreground">No analyses yet. Paste a YouTube URL above to begin.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {analyses.map((a) => (
              <Link
                key={a.id}
                to="/analysis/$id"
                params={{ id: a.id }}
                className="group rounded-xl overflow-hidden border border-border bg-surface/40 hover:bg-surface/70 hover:border-primary/40 transition-all"
              >
                <div className="aspect-video relative bg-muted overflow-hidden">
                  {a.thumbnail_url && (
                    <img src={a.thumbnail_url} alt={a.title ?? ""} className="h-full w-full object-cover" />
                  )}
                  <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent" />
                  <div className="absolute top-2 right-2 rounded-full bg-background/80 backdrop-blur px-2 py-0.5 text-xs capitalize">
                    {a.status}
                  </div>
                  <Play className="absolute inset-0 m-auto h-10 w-10 text-white/0 group-hover:text-white/90 transition-colors" />
                </div>
                <div className="p-4">
                  <h3 className="font-medium line-clamp-2 leading-snug">{a.title ?? a.youtube_url}</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {a.channel} · {formatDistanceToNow(new Date(a.created_at), { addSuffix: true })}
                  </p>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
