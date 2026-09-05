/**
 * Public Evidence Verification Tool
 * Standalone page where anyone can verify the integrity of evidence
 * by entering an analysis ID. No login required.
 */

import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Shield,
  ShieldCheck,
  ShieldAlert,
  Loader2,
  Hash,
  Search,
  Lock,
} from "lucide-react";

const WORKER_URL =
  import.meta.env.VITE_WORKER_URL ||
  "https://tubescribe-backend-production.up.railway.app";

export const Route = createFileRoute("/verify")({
  component: PublicVerify,
});

interface VerifyResult {
  verified: boolean;
  error?: string;
  evidence_hash?: { original: string; recomputed: string; match: boolean };
  raw_hash?: { original: string; recomputed: string; match: boolean };
  captured_at?: string;
}

function PublicVerify() {
  const [analysisId, setAnalysisId] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<VerifyResult | null>(null);

  const verify = async () => {
    if (!analysisId.trim()) return;
    setLoading(true);
    setResult(null);

    try {
      const res = await fetch(`${WORKER_URL}/verify-hash`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ analysis_id: analysisId.trim() }),
      });
      const data: VerifyResult = await res.json();
      setResult(data);
    } catch {
      setResult({ verified: false, error: "Unable to reach verification server. Please try again." });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <SiteHeader />
      <div className="container mx-auto px-4 py-8 max-w-2xl">
        <div className="flex items-center gap-3 mb-2">
          <Lock className="h-8 w-8 text-primary" />
          <h1 className="text-3xl font-bold">Evidence Verification Tool</h1>
        </div>
        <p className="text-muted-foreground mb-6">
          Public, no-login tool to verify the SHA-256 integrity of any evidence
          analyzed through the LegalLens system. Enter the analysis ID from any
          forensic report or custody record.
        </p>

        <Card className="mb-6">
          <CardContent className="pt-6">
            <div className="flex gap-3">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <input
                  type="text"
                  value={analysisId}
                  onChange={(e) => setAnalysisId(e.target.value)}
                  placeholder="Paste analysis ID here..."
                  className="w-full pl-10 pr-4 py-2 rounded-lg border border-input bg-background text-foreground"
                  onKeyDown={(e) => e.key === "Enter" && verify()}
                />
              </div>
              <Button onClick={verify} disabled={loading || !analysisId.trim()}>
                {loading ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Shield className="h-4 w-4 mr-2" />}
                {loading ? "Verifying..." : "Verify"}
              </Button>
            </div>
          </CardContent>
        </Card>

        {result && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                {result.verified ? (
                  <ShieldCheck className="h-6 w-6 text-emerald-600" />
                ) : (
                  <ShieldAlert className="h-6 w-6 text-red-600" />
                )}
                {result.verified ? "Evidence Verified" : "Verification Failed"}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {result.error && !result.verified && (
                <p className="text-sm text-red-600">{result.error}</p>
              )}

              {result.verified && (
                <div className="p-4 rounded-lg bg-emerald-50 dark:bg-emerald-950/30 border border-emerald-200 dark:border-emerald-800">
                  <p className="font-medium text-emerald-700 dark:text-emerald-400">
                    ✓ The evidence has not been modified since it was captured.
                  </p>
                  {result.captured_at && (
                    <p className="text-xs text-muted-foreground mt-1">
                      Originally captured: {new Date(result.captured_at).toLocaleString()}
                    </p>
                  )}
                </div>
              )}

              {result.evidence_hash && (
                <div className="space-y-2">
                  <p className="text-sm font-semibold flex items-center gap-1">
                    <Hash className="h-3 w-3" /> Evidence Hash
                  </p>
                  <div className="p-3 rounded-lg border text-xs font-mono space-y-1">
                    <div>
                      <span className="text-muted-foreground">Original:  </span>
                      <span className="break-all">{result.evidence_hash.original}</span>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Recomputed: </span>
                      <span className="break-all">{result.evidence_hash.recomputed}</span>
                    </div>
                  </div>
                  <Badge variant={result.evidence_hash.match ? "default" : "destructive"}>
                    {result.evidence_hash.match ? "Hash Match" : "Hash Mismatch"}
                  </Badge>
                </div>
              )}

              {result.raw_hash && (
                <div className="space-y-2">
                  <p className="text-sm font-semibold flex items-center gap-1">
                    <Hash className="h-3 w-3" /> Raw Transcript Hash
                  </p>
                  <div className="p-3 rounded-lg border text-xs font-mono space-y-1">
                    <div>
                      <span className="text-muted-foreground">Original:  </span>
                      <span className="break-all">{result.raw_hash.original}</span>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Recomputed: </span>
                      <span className="break-all">{result.raw_hash.recomputed}</span>
                    </div>
                  </div>
                  <Badge variant={result.raw_hash.match ? "default" : "destructive"}>
                    {result.raw_hash.match ? "Hash Match" : "Hash Mismatch"}
                  </Badge>
                </div>
              )}
            </CardContent>
          </Card>
        )}

        <div className="mt-8 text-center text-xs text-muted-foreground">
          <p>This tool uses SHA-256 cryptographic hashing to verify evidence integrity.</p>
          <p>No account required. Verification results are deterministic and reproducible.</p>
        </div>
      </div>
    </div>
  );
}

export default PublicVerify;
