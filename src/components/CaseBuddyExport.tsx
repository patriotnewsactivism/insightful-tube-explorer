import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Loader2, Scale, Download, Copy, Check, ExternalLink } from "lucide-react";
import { toast } from "sonner";

const WORKER_URL = import.meta.env.VITE_WORKER_URL || "https://insightful-tube-explorer-production.up.railway.app";
const CASEBUDDY_URL = import.meta.env.VITE_CASEBUDDY_URL || "https://casebuddy.donmatthews.live";

type CaseBuddyExportProps = {
  analysisId: string;
  title: string;
};

export function CaseBuddyExportButton({ analysisId, title }: CaseBuddyExportProps) {
  const [exporting, setExporting] = useState(false);
  const [exportData, setExportData] = useState<any>(null);
  const [copied, setCopied] = useState(false);

  async function handleExport() {
    setExporting(true);
    try {
      const res = await fetch(`${WORKER_URL}/casebuddy-export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ analysis_id: analysisId }),
      });
      const data = await res.json();
      if (data.error) {
        toast.error(data.error);
        return;
      }
      setExportData(data);
      toast.success("CaseBuddy evidence package ready!");
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setExporting(false);
    }
  }

  function handleCopyJson() {
    if (!exportData) return;
    navigator.clipboard.writeText(JSON.stringify(exportData, null, 2));
    setCopied(true);
    toast.success("Evidence JSON copied to clipboard");
    setTimeout(() => setCopied(false), 2000);
  }

  function handleDownload() {
    if (!exportData) return;
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeName = (title || "evidence").replace(/[^a-z0-9]/gi, "_").slice(0, 60);
    a.download = `CaseBuddy_Evidence_${safeName}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast.success("Evidence file downloaded!");
  }

  function handleOpenCaseBuddy() {
    window.open(CASEBUDDY_URL, "_blank");
  }

  if (exportData) {
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-2 text-sm text-emerald-400">
          <Check className="h-4 w-4" />
          <span className="font-medium">Evidence package ready</span>
        </div>
        <div className="rounded-md border border-border/50 bg-muted/30 p-3">
          <div className="text-xs text-muted-foreground space-y-1">
            <p><span className="font-medium text-foreground">{exportData.evidence?.name}</span></p>
            <p>Type: {exportData.evidence?.type}</p>
            <p>Status: <span className={exportData.forensic?.integrity_status === "verified" ? "text-emerald-400" : "text-amber-400"}>{exportData.forensic?.integrity_status}</span></p>
            <p>Quotes: {exportData.insights?.quotes?.length || 0} · Timeline: {exportData.insights?.timeline?.length || 0} · Contradictions: {exportData.insights?.contradictions?.length || 0}</p>
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button variant="outline" size="sm" onClick={handleCopyJson}>
            {copied ? <Check className="h-3.5 w-3.5 mr-1" /> : <Copy className="h-3.5 w-3.5 mr-1" />}
            {copied ? "Copied" : "Copy JSON"}
          </Button>
          <Button variant="outline" size="sm" onClick={handleDownload}>
            <Download className="h-3.5 w-3.5 mr-1" />
            Download
          </Button>
          <Button variant="default" size="sm" onClick={handleOpenCaseBuddy}>
            <ExternalLink className="h-3.5 w-3.5 mr-1" />
            Open CaseBuddy
          </Button>
        </div>
      </div>
    );
  }

  return (
    <Button variant="outline" size="sm" onClick={handleExport} disabled={exporting}>
      {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : <Scale className="h-3.5 w-3.5 mr-1" />}
      Send to CaseBuddy
    </Button>
  );
}
