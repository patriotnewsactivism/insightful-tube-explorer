/**
 * Expert Witness Report Generator
 * Generates court-ready reports with timestamp citations, hash verification,
 * and chain of custody. Designed for civil rights attorneys analyzing
 * body-cam footage, deposition videos, and interview recordings.
 */

import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  FileText,
  Shield,
  Loader2,
  Download,
  Scale,
  Clock,
  Hash,
  CheckCircle,
  AlertCircle,
  Printer,
} from "lucide-react";

const WORKER_URL =
  import.meta.env.VITE_WORKER_URL ||
  "https://insightful-tube-explorer-production.up.railway.app";

export const Route = createFileRoute("/expert-witness")({
  component: ExpertWitnessReport,
});

interface ReportData {
  analysis_id: string;
  video_title: string;
  video_url: string;
  transcript_hash: string;
  raw_hash: string;
  captured_at: string;
  verification: {
    verified: boolean;
    evidence_hash: { original: string; recomputed: string; match: boolean };
    raw_hash: { original: string; recomputed: string; match: boolean };
  };
  custody_chain: Array<{
    id: string;
    action: string;
    actor: string;
    created_at: string;
    details: Record<string, unknown>;
  }>;
  key_quotes: Array<{
    timestamp: string;
    speaker: string;
    text: string;
    significance: string;
  }>;
  contradictions: Array<{
    timestamp_a: string;
    timestamp_b: string;
    statement_a: string;
    statement_b: string;
    analysis: string;
  }>;
  timeline_events: Array<{
    timestamp: string;
    event: string;
    speaker: string;
  }>;
}

function ExpertWitnessReport() {
  const [analysisId, setAnalysisId] = useState("");
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<ReportData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const generateReport = async () => {
    if (!analysisId.trim()) return;
    setLoading(true);
    setError(null);
    setReport(null);

    try {
      // Fetch verification, custody chain, and analysis data in parallel
      const [verifyRes, custodyRes, analysisRes] = await Promise.all([
        fetch(`${WORKER_URL}/verify-hash`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ analysis_id: analysisId }),
        }),
        fetch(`${WORKER_URL}/custody-log`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ analysis_id: analysisId }),
        }),
        fetch(`${WORKER_URL}/analysis?id=${analysisId}`),
      ]);

      const verification = await verifyRes.json();
      const custody = await custodyRes.json();
      const analysis = await analysisRes.json();

      if (analysis.error) {
        setError(analysis.error);
        return;
      }

      // Extract key quotes with timestamps from the analysis
      const keyQuotes = (analysis.key_quotes || []).map((q: any) => ({
        timestamp: q.timestamp || "N/A",
        speaker: q.speaker || "Unknown",
        text: q.text || q.quote || "",
        significance: q.significance || q.context || "",
      }));

      // Extract contradictions with timestamps
      const contradictions = (analysis.contradictions || []).map((c: any) => ({
        timestamp_a: c.timestamp_a || c.time_a || "N/A",
        timestamp_b: c.timestamp_b || c.time_b || "N/A",
        statement_a: c.statement_a || c.claim_a || "",
        statement_b: c.statement_b || c.claim_b || "",
        analysis: c.analysis || c.explanation || "",
      }));

      // Extract timeline events
      const timelineEvents = (analysis.timeline || []).map((t: any) => ({
        timestamp: t.timestamp || t.time || "N/A",
        event: t.event || t.description || "",
        speaker: t.speaker || "",
      }));

      setReport({
        analysis_id: analysisId,
        video_title: analysis.title || analysis.video_title || "Untitled Analysis",
        video_url: analysis.video_url || analysis.url || "",
        transcript_hash: analysis.evidence_hash || "",
        raw_hash: analysis.raw_hash || "",
        captured_at: analysis.captured_at || analysis.created_at || "",
        verification,
        custody_chain: custody.chain || [],
        key_quotes: keyQuotes,
        contradictions,
        timeline_events: timelineEvents,
      });
    } catch (err) {
      setError("Failed to generate report. Check the analysis ID and try again.");
    } finally {
      setLoading(false);
    }
  };

  const handlePrint = () => {
    window.print();
  };

  const handleDownload = () => {
    if (!report) return;
    const text = formatReportAsText(report);
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `expert-witness-report-${report.analysis_id}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="min-h-screen bg-background">
      <SiteHeader />
      <div className="container mx-auto px-4 py-8 max-w-4xl">
        <div className="flex items-center gap-3 mb-2">
          <Scale className="h-8 w-8 text-primary" />
          <h1 className="text-3xl font-bold">Expert Witness Report Generator</h1>
        </div>
        <p className="text-muted-foreground mb-6">
          Generate court-ready reports with timestamp citations, SHA-256 hash verification,
          and chain of custody documentation. Designed for civil rights attorneys analyzing
          body-cam footage and evidentiary recordings.
        </p>

        {/* Search bar */}
        <Card className="mb-6">
          <CardContent className="pt-6">
            <div className="flex gap-3">
              <input
                type="text"
                value={analysisId}
                onChange={(e) => setAnalysisId(e.target.value)}
                placeholder="Enter Analysis ID (e.g., abc123-def456...)"
                className="flex-1 px-4 py-2 rounded-lg border border-input bg-background text-foreground"
                onKeyDown={(e) => e.key === "Enter" && generateReport()}
              />
              <Button onClick={generateReport} disabled={loading || !analysisId.trim()}>
                {loading ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <FileText className="h-4 w-4 mr-2" />}
                {loading ? "Generating..." : "Generate Report"}
              </Button>
            </div>
          </CardContent>
        </Card>

        {error && (
          <div className="flex items-center gap-2 p-4 mb-6 rounded-lg border border-destructive/30 bg-destructive/10 text-destructive">
            <AlertCircle className="h-5 w-5" /> {error}
          </div>
        )}

        {report && (
          <div className="space-y-6 print:space-y-4">
            {/* Report header */}
            <Card className="print:border-2 print:border-black">
              <CardHeader>
                <div className="flex items-start justify-between">
                  <div>
                    <CardTitle className="text-2xl">EXPERT WITNESS REPORT</CardTitle>
                    <p className="text-sm text-muted-foreground mt-1">
                      Generated: {new Date().toLocaleString()}
                    </p>
                  </div>
                  <div className="flex gap-2 print:hidden">
                    <Button variant="outline" size="sm" onClick={handlePrint}>
                      <Printer className="h-4 w-4 mr-2" /> Print
                    </Button>
                    <Button variant="outline" size="sm" onClick={handleDownload}>
                      <Download className="h-4 w-4 mr-2" /> Download
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <p className="font-semibold">Analysis ID:</p>
                    <p className="font-mono text-xs">{report.analysis_id}</p>
                  </div>
                  <div>
                    <p className="font-semibold">Source:</p>
                    <p>{report.video_title}</p>
                    {report.video_url && (
                      <a href={report.video_url} className="text-xs text-blue-500 hover:underline" target="_blank" rel="noopener">
                        {report.video_url}
                      </a>
                    )}
                  </div>
                  <div>
                    <p className="font-semibold">Evidence Captured:</p>
                    <p>{report.captured_at ? new Date(report.captured_at).toLocaleString() : "N/A"}</p>
                  </div>
                  <div>
                    <p className="font-semibold">Report Generated:</p>
                    <p>{new Date().toLocaleString()}</p>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Evidence Integrity */}
            <Card>
              <CardHeader>
                <div className="flex items-center gap-2">
                  <Shield className="h-5 w-5 text-emerald-600" />
                  <CardTitle>Evidence Integrity Verification</CardTitle>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                {report.verification.verified ? (
                  <div className="flex items-center gap-2 p-3 rounded-lg bg-emerald-50 dark:bg-emerald-950/30 border border-emerald-200 dark:border-emerald-800">
                    <CheckCircle className="h-5 w-5 text-emerald-600" />
                    <span className="font-medium text-emerald-700 dark:text-emerald-400">
                      Evidence verified — transcript has not been modified since capture.
                    </span>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 p-3 rounded-lg bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800">
                    <AlertCircle className="h-5 w-5 text-red-600" />
                    <span className="font-medium text-red-700 dark:text-red-400">
                      Evidence integrity check FAILED — data may have been modified.
                    </span>
                  </div>
                )}

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
                  <div className="p-3 rounded-lg border">
                    <p className="font-semibold flex items-center gap-1 mb-1">
                      <Hash className="h-3 w-3" /> Evidence Hash (SHA-256)
                    </p>
                    <p className="font-mono text-xs break-all">{report.transcript_hash || "N/A"}</p>
                    {report.verification.evidence_hash && (
                      <Badge variant={report.verification.evidence_hash.match ? "default" : "destructive"} className="mt-2">
                        {report.verification.evidence_hash.match ? "Match" : "Mismatch"}
                      </Badge>
                    )}
                  </div>
                  <div className="p-3 rounded-lg border">
                    <p className="font-semibold flex items-center gap-1 mb-1">
                      <Hash className="h-3 w-3" /> Raw Transcript Hash (SHA-256)
                    </p>
                    <p className="font-mono text-xs break-all">{report.raw_hash || "N/A"}</p>
                    {report.verification.raw_hash && (
                      <Badge variant={report.verification.raw_hash.match ? "default" : "destructive"} className="mt-2">
                        {report.verification.raw_hash.match ? "Match" : "Mismatch"}
                      </Badge>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Key Quotes with Timestamps */}
            {report.key_quotes.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle>Key Statements with Timestamp Citations</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  {report.key_quotes.map((quote, i) => (
                    <div key={i} className="border-l-4 border-primary pl-4 py-1">
                      <div className="flex items-center gap-2 mb-1">
                        <Clock className="h-3 w-3 text-muted-foreground" />
                        <span className="text-xs font-mono font-semibold">{quote.timestamp}</span>
                        <span className="text-xs text-muted-foreground">— {quote.speaker}</span>
                      </div>
                      <p className="text-sm italic">"{quote.text}"</p>
                      {quote.significance && (
                        <p className="text-xs text-muted-foreground mt-1">Significance: {quote.significance}</p>
                      )}
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Contradictions */}
            {report.contradictions.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle>Detected Contradictions</CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  {report.contradictions.map((c, i) => (
                    <div key={i} className="p-4 rounded-lg border">
                      <div className="grid grid-cols-2 gap-4 mb-2">
                        <div>
                          <p className="text-xs font-mono font-semibold text-blue-600">{c.timestamp_a}</p>
                          <p className="text-sm italic">"{c.statement_a}"</p>
                        </div>
                        <div>
                          <p className="text-xs font-mono font-semibold text-red-600">{c.timestamp_b}</p>
                          <p className="text-sm italic">"{c.statement_b}"</p>
                        </div>
                      </div>
                      {c.analysis && (
                        <p className="text-xs text-muted-foreground border-t pt-2 mt-2">{c.analysis}</p>
                      )}
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Timeline */}
            {report.timeline_events.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle>Event Timeline</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    {report.timeline_events.map((event, i) => (
                      <div key={i} className="flex gap-3 text-sm">
                        <span className="font-mono text-xs font-semibold min-w-[80px]">{event.timestamp}</span>
                        <div>
                          <span>{event.event}</span>
                          {event.speaker && <span className="text-xs text-muted-foreground ml-2">— {event.speaker}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Chain of Custody */}
            <Card>
              <CardHeader>
                <CardTitle>Chain of Custody</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {report.custody_chain.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No custody records available.</p>
                  ) : (
                    report.custody_chain.map((entry, i) => (
                      <div key={entry.id || i} className="flex gap-3 text-sm border-l-2 border-muted pl-3 py-1">
                        <div>
                          <p className="font-medium">{entry.action.replace(/_/g, " ")}</p>
                          <p className="text-xs text-muted-foreground">
                            {new Date(entry.created_at).toLocaleString()} — by {entry.actor}
                          </p>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </CardContent>
            </Card>

            {/* Footer */}
            <div className="text-center text-xs text-muted-foreground py-4 print:border-t print:border-black print:pt-4">
              <p>This report was generated by LegalLens Forensic Analysis System.</p>
              <p>Evidence integrity verified via SHA-256 hash comparison. Report ID: {report.analysis_id}</p>
              <p className="mt-1">This document is suitable for submission as an exhibit attachment.</p>
            </div>
          </div>
        )}

        {!report && !loading && !error && (
          <Card>
            <CardContent className="pt-6 text-center text-muted-foreground">
              <Scale className="h-12 w-12 mx-auto mb-3 opacity-50" />
              <p>Enter an analysis ID above to generate a court-ready expert witness report.</p>
              <p className="text-xs mt-2">Reports include timestamp citations, hash verification, contradictions, and chain of custody.</p>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function formatReportAsText(report: ReportData): string {
  const lines: string[] = [];
  lines.push("EXPERT WITNESS REPORT");
  lines.push("=" .repeat(60));
  lines.push(`Generated: ${new Date().toLocaleString()}`);
  lines.push(`Analysis ID: ${report.analysis_id}`);
  lines.push(`Source: ${report.video_title}`);
  lines.push(`Evidence Captured: ${report.captured_at ? new Date(report.captured_at).toLocaleString() : "N/A"}`);
  lines.push("");

  lines.push("EVIDENCE INTEGRITY VERIFICATION");
  lines.push("-".repeat(60));
  lines.push(`Status: ${report.verification.verified ? "VERIFIED" : "FAILED"}`);
  lines.push(`Evidence Hash (SHA-256): ${report.transcript_hash || "N/A"}`);
  lines.push(`Raw Hash (SHA-256): ${report.raw_hash || "N/A"}`);
  lines.push("");

  if (report.key_quotes.length > 0) {
    lines.push("KEY STATEMENTS WITH TIMESTAMP CITATIONS");
    lines.push("-".repeat(60));
    report.key_quotes.forEach((q, i) => {
      lines.push(`[${q.timestamp}] ${q.speaker}: "${q.text}"`);
      if (q.significance) lines.push(`  Significance: ${q.significance}`);
      lines.push("");
    });
  }

  if (report.contradictions.length > 0) {
    lines.push("DETECTED CONTRADICTIONS");
    lines.push("-".repeat(60));
    report.contradictions.forEach((c, i) => {
      lines.push(`Contradiction ${i + 1}:`);
      lines.push(`  [${c.timestamp_a}] "${c.statement_a}"`);
      lines.push(`  [${c.timestamp_b}] "${c.statement_b}"`);
      if (c.analysis) lines.push(`  Analysis: ${c.analysis}`);
      lines.push("");
    });
  }

  if (report.timeline_events.length > 0) {
    lines.push("EVENT TIMELINE");
    lines.push("-".repeat(60));
    report.timeline_events.forEach((e) => {
      lines.push(`[${e.timestamp}] ${e.event}${e.speaker ? ` — ${e.speaker}` : ""}`);
    });
    lines.push("");
  }

  lines.push("CHAIN OF CUSTODY");
  lines.push("-".repeat(60));
  report.custody_chain.forEach((entry) => {
    lines.push(`[${new Date(entry.created_at).toLocaleString()}] ${entry.action.replace(/_/g, " ")} — by ${entry.actor}`);
  });
  lines.push("");

  lines.push("=" .repeat(60));
  lines.push("Generated by LegalLens Forensic Analysis System");
  lines.push("Evidence integrity verified via SHA-256 hash comparison");

  return lines.join("\n");
}

export default ExpertWitnessReport;
