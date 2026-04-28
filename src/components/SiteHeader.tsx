import { Link } from "@tanstack/react-router";
import { useAuth } from "@/hooks/useAuth";
import { Button } from "@/components/ui/button";
import { LogOut } from "lucide-react";

function TubeScribeLogo({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 40" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <rect width="40" height="40" rx="10" fill="url(#ts-grad)" />
      <path d="M16 12L28 20L16 28V12Z" fill="white" opacity="0.95" />
      <path d="M12 30L14 22L20 26L12 30Z" fill="white" opacity="0.7" />
      <defs>
        <linearGradient id="ts-grad" x1="0" y1="0" x2="40" y2="40">
          <stop stopColor="#EF4444" />
          <stop offset="1" stopColor="#B91C1C" />
        </linearGradient>
      </defs>
    </svg>
  );
}

export function SiteHeader() {
  const { user, signOut } = useAuth();
  return (
    <header className="border-b border-border/60 bg-background/80 backdrop-blur-md sticky top-0 z-50">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <Link to="/" className="flex items-center gap-2.5 group">
          <TubeScribeLogo />
          <span className="font-display text-xl font-bold tracking-tight">
            Tube<span className="text-red-500">Scribe</span>
          </span>
        </Link>
        <nav className="flex items-center gap-2">
          {user ? (
            <>
              <Button asChild variant="ghost" size="sm">
                <Link to="/dashboard">Dashboard</Link>
              </Button>
              <Button variant="ghost" size="sm" onClick={signOut}>
                <LogOut className="h-4 w-4" />
              </Button>
            </>
          ) : (
            <>
              <Button asChild variant="ghost" size="sm">
                <Link to="/auth">Sign in</Link>
              </Button>
              <Button asChild size="sm" className="bg-red-600 hover:bg-red-700 text-white">
                <Link to="/auth">Get started free</Link>
              </Button>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
