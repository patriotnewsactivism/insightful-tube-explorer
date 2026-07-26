// Vercel serverless function: server-side verification that a Stripe
// Checkout session was actually paid. The client (still authenticated
// with its own Supabase session) uses this as proof before writing
// profiles.is_unlimited = true to its OWN row -- avoids needing a
// Supabase service_role key in this function at all.
export default async function handler(req, res) {
  const sessionId = req.method === "GET" ? req.query.session_id : req.body?.session_id;
  if (!sessionId) return res.status(400).json({ error: "session_id required" });

  const secretKey = process.env.STRIPE_SECRET_KEY;
  if (!secretKey) return res.status(500).json({ error: "Stripe not configured" });

  const resp = await fetch(`https://api.stripe.com/v1/checkout/sessions/${sessionId}`, {
    headers: { Authorization: `Bearer ${secretKey}` },
  });
  const session = await resp.json();
  if (!resp.ok) return res.status(500).json({ error: session.error?.message || "Stripe error" });

  const paid = session.payment_status === "paid" && session.mode === "subscription";
  return res.status(200).json({ paid, userId: session.client_reference_id || session.metadata?.user_id || null });
}
