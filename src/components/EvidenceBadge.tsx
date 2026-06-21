import { useState } from "react";
import { Shield, ShieldAlert, ShieldQuestion, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const WORKER_URL =
  import.meta.env.VITE_WORKER_URL ||
  "https://insightful-tube-explorer-production.up.railway.app";

type EvidenceBadgeProps = {
  analysisId: string;
  evidenceHash: string | null;
  capturedAt: string | null;
  /** Compact mode for dashboard cards */
  compact?: boolean;
};

type VerifyResult = {
  verified: boolean;
  error?: string;
  evidence_hash?: { original: string; recomputed: string; match: boolean };
  raw_hash?: { original: string; recomputed: string; match: boolean };
  captured_at?: string;
};

export function EvidenceBadge({
  analysisId,
  evidenceHash,
  capturedAt,
  compact = false,
}: EvidenceBadgeProps) {
  const [verifyState, setVerifyState] = useState<
    "idle" | "loading" | "verified" | "failed" | "no-data"
  >(evidenceHash ? "idle" : "no-data");
  const [result, setResult] = useState<VerifyResult | null>(null);

  async function verify() {
    if (verifyState === "loading") return;
    setVerifyState("loading");
    try {
      const res = await fetch(`${WORKER_URL}/verify-hash`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ analysis_id: analysisId }),
      });
      const data: VerifyResult = await res.json();
      setResult(data);
      setVerifyState(data.verified ? "verified" : "failed");
    } catch {
      setVerifyState("failed");
      setResult({ verified: false, error: "Network error" });
    }
  }

  if (verifyState === "no-data") {
    if (compact) return null;
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger>
            <Badge
              variant="outline"
              className="gap-1 text-gray-400 border-gray-300 dark:border-gray-700"
            >
              <ShieldQuestion className="h-3 w-3" />
              {!compact && "Pre-forensic"}
            </Badge>
          </TooltipTrigger>
          <TooltipContent>
            <p>This analysis was created before forensic tracking was enabled.</p>
            <p className="text-xs text-muted-foreground mt-1">
              Re-process the analysis to generate forensic data.
            </p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  const Icon =
    verifyState === "loading"
      ? Loader2
      : verifyState === "verified"
        ? Shield
        : verifyState === "failed"
          ? ShieldAlert
          : Shield;

  const colors =
    verifyState === "verified"
      ? "text-emerald-600 border-emerald-300 bg-emerald-50 dark:bg-emerald-950/30 dark:border-emerald-800 dark:text-emerald-400"
      : verifyState === "failed"
        ? "text-red-600 border-red-300 bg-red-50 dark:bg-red-950/30 dark:border-red-800 dark:text-red-400"
        : "text-blue-600 border-blue-300 bg-blue-50 dark:bg-blue-950/30 dark:border-blue-800 dark:text-blue-400";

  const label =
    verifyState === "loading"
      ? "Verifying…"
      : verifyState === "verified"
        ? "Evidence Verified"
        : verifyState === "failed"
          ? "Integrity Alert"
          : "Verify Evidence";

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button onClick={verify} disabled={verifyState === "loading"}>
            <Badge variant="outline" className={`gap-1 cursor-pointer ${colors}`}>
              <Icon
                className={`h-3 w-3 ${verifyState === "loading" ? "animate-spin" : ""}`}
              />
              {!compact && label}
            </Badge>
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-sm">
          {verifyState === "idle" && (
            <p>Click to verify SHA-256 integrity of the original transcript.</p>
          )}
          {verifyState === "verified" && result && (
            <div className="space-y-1">
              <p className="font-medium text-emerald-600">
                ✓ Transcript has not been modified since capture.
              </p>
              <p className="text-xs text-muted-foreground">
                Captured: {capturedAt ? new Date(capturedAt).toLocaleString() : "N/A"}
              </p>
              <p className="text-xs font-mono break-all text-muted-foreground">
                Hash: {evidenceHash?.slice(0, 16)}…
              </p>
            </div>
          )}
          {verifyState === "failed" && result && (
            <div className="space-y-1">
              <p className="font-medium text-red-600">
                ✗ {result.error || "Evidence integrity check failed."}
              </p>
              {result.evidence_hash && !result.evidence_hash.match && (
                <p className="text-xs text-muted-foreground">
                  Evidence hash mismatch — data may have been modified.
                </p>
              )}
            </div>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
