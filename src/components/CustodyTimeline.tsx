import { useEffect, useState } from "react";
import { Loader2, FileText, Brain, Sparkles, Download, Shield, RefreshCw, Eye } from "lucide-react";
import { Badge } from "@/components/ui/badge";

const WORKER_URL =
  import.meta.env.VITE_WORKER_URL ||
  "https://tubescribe-backend-production.up.railway.app";

type CustodyEntry = {
  id: string;
  analysis_id: string;
  action: string;
  details: Record<string, unknown>;
  actor: string;
  created_at: string;
};

const ACTION_META: Record<
  string,
  { label: string; icon: typeof FileText; color: string }
> = {
  transcript_captured: {
    label: "Transcript Captured",
    icon: FileText,
    color: "text-blue-500",
  },
  ai_processing_started: {
    label: "AI Processing Started",
    icon: Brain,
    color: "text-purple-500",
  },
  ai_processed: {
    label: "AI Processing Complete",
    icon: Sparkles,
    color: "text-purple-600",
  },
  enrichment_complete: {
    label: "Enrichment Complete",
    icon: Sparkles,
    color: "text-indigo-500",
  },
  exported: {
    label: "Exported",
    icon: Download,
    color: "text-green-500",
  },
  forensic_exported: {
    label: "Forensic Export",
    icon: Shield,
    color: "text-emerald-600",
  },
  hash_verified: {
    label: "Hash Verified",
    icon: Shield,
    color: "text-emerald-500",
  },
  reprocessed: {
    label: "Reprocessed",
    icon: RefreshCw,
    color: "text-amber-500",
  },
  edited: {
    label: "Edited",
    icon: Eye,
    color: "text-orange-500",
  },
};

export function CustodyTimeline({ analysisId }: { analysisId: string }) {
  const [entries, setEntries] = useState<CustodyEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${WORKER_URL}/custody-log`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ analysis_id: analysisId }),
        });
        const data = await res.json();
        setEntries(data.chain || []);
      } catch (e) {
        console.error("Failed to load custody log:", e);
      } finally {
        setLoading(false);
      }
    })();
  }, [analysisId]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground py-4">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading chain of custody…
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-4">
        No custody records available. This analysis may predate the forensic system.
      </p>
    );
  }

  return (
    <div className="relative">
      {/* Vertical line */}
      <div className="absolute left-4 top-2 bottom-2 w-px bg-border" />

      <div className="space-y-4">
        {entries.map((entry, i) => {
          const meta = ACTION_META[entry.action] || {
            label: entry.action.replace(/_/g, " "),
            icon: Eye,
            color: "text-gray-500",
          };
          const Icon = meta.icon;
          const ts = new Date(entry.created_at);
          const details = entry.details || {};

          return (
            <div key={entry.id} className="relative flex items-start gap-3 pl-2">
              {/* Dot on the timeline */}
              <div
                className={`relative z-10 flex h-5 w-5 items-center justify-center rounded-full bg-background border-2 ${meta.color.replace("text-", "border-")}`}
              >
                <Icon className={`h-3 w-3 ${meta.color}`} />
              </div>

              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium">{meta.label}</span>
                  <Badge variant="outline" className="text-xs px-1.5 py-0">
                    {entry.actor}
                  </Badge>
                </div>

                <p className="text-xs text-muted-foreground mt-0.5">
                  {ts.toLocaleDateString()} {ts.toLocaleTimeString()}
                </p>

                {/* Show interesting details */}
                {Object.keys(details).length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {details.source && (
                      <Badge variant="secondary" className="text-xs">
                        Source: {String(details.source)}
                      </Badge>
                    )}
                    {details.evidence_hash && (
                      <Badge variant="secondary" className="text-xs font-mono">
                        Hash: {String(details.evidence_hash).slice(0, 12)}…
                      </Badge>
                    )}
                    {details.char_count && (
                      <Badge variant="secondary" className="text-xs">
                        {Number(details.char_count).toLocaleString()} chars
                      </Badge>
                    )}
                    {details.primary_model && (
                      <Badge variant="secondary" className="text-xs">
                        Model: {String(details.primary_model)}
                      </Badge>
                    )}
                    {details.format && (
                      <Badge variant="secondary" className="text-xs">
                        Format: {String(details.format)}
                      </Badge>
                    )}
                    {details.verified !== undefined && (
                      <Badge
                        variant="secondary"
                        className={`text-xs ${details.verified ? "text-emerald-600" : "text-red-600"}`}
                      >
                        {details.verified ? "✓ Verified" : "✗ Failed"}
                      </Badge>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
