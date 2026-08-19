import { NextRequest, NextResponse } from "next/server";

// All browser traffic to the AHSEA control plane goes through this route.
// Two things live here that must never reach the client bundle:
//   1. API_BASE_URL — where the real backend is.
//   2. AHSEA_API_KEY — the credential the backend expects in X-API-Key.
// Neither is prefixed with NEXT_PUBLIC_, so Next.js keeps them server-only.

const API_BASE_URL = process.env.API_BASE_URL || "http://localhost:8000";
const API_KEY = process.env.AHSEA_API_KEY || "";

async function forward(req: NextRequest, path: string[]): Promise<NextResponse> {
  const targetPath = "/" + path.join("/");
  const search = req.nextUrl.search;
  const url = `${API_BASE_URL}${targetPath}${search}`;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (API_KEY) headers["X-API-Key"] = API_KEY;

  const hasBody = !["GET", "HEAD"].includes(req.method);
  const init: RequestInit = {
    method: req.method,
    headers,
    cache: "no-store",
  };
  if (hasBody) {
    const body = await req.text();
    if (body) init.body = body;
  }

  try {
    const upstream = await fetch(url, init);
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("Content-Type") || "application/json" },
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `Could not reach AHSEA backend at ${API_BASE_URL}. Is it running?` },
      { status: 502 },
    );
  }
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path);
}
export async function POST(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path);
}
export async function PUT(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path);
}
export async function PATCH(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path);
}
export async function DELETE(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path);
}
