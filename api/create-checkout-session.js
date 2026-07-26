// Vercel serverless function: creates a Stripe Checkout session for
// TubeScribe Pro ($9/mo unlimited analyses). Uses raw fetch against the
// Stripe REST API (no SDK dependency needed).
export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "Method not allowed" });
  const { userId, origin } = req.body || {};
  if (!userId || !origin) return res.status(400).json({ error: "userId and origin required" });

  const secretKey = process.env.STRIPE_SECRET_KEY;
  const priceId = process.env.STRIPE_TUBESCRIBE_PRO_PRICE_ID;
  if (!secretKey || !priceId) return res.status(500).json({ error: "Stripe not configured" });

  const body = new URLSearchParams({
    mode: "subscription",
    "line_items[0][price]": priceId,
    "line_items[0][quantity]": "1",
    success_url: `${origin}/dashboard?upgrade=success&session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${origin}/dashboard?upgrade=cancelled`,
    client_reference_id: userId,
    "metadata[user_id]": userId,
  });

  const resp = await fetch("https://api.stripe.com/v1/checkout/sessions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${secretKey}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });
  const session = await resp.json();
  if (!resp.ok) return res.status(500).json({ error: session.error?.message || "Stripe error" });
  return res.status(200).json({ url: session.url });
}
