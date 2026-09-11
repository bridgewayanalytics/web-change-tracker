import { type NextRequest, NextResponse } from "next/server";
import { auth0 } from "@/lib/auth0";

const AUTH0_ENABLED = !!(
  process.env.AUTH0_SECRET &&
  process.env.AUTH0_DOMAIN &&
  process.env.AUTH0_CLIENT_ID &&
  process.env.AUTH0_CLIENT_SECRET &&
  process.env.APP_BASE_URL
);

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Auth0 handles its own routes (login / logout / callback)
  if (pathname.startsWith("/auth")) {
    if (!AUTH0_ENABLED) return NextResponse.next();
    return await auth0.middleware(request);
  }

  // Skip Next.js internals, static files, and API routes (ALB health checks hit /api/pages)
  if (
    pathname.startsWith("/_next") ||
    pathname.startsWith("/api") ||
    pathname === "/icon.png" ||
    pathname === "/favicon.ico"
  ) {
    return NextResponse.next();
  }

  // If Auth0 is not configured, skip auth entirely
  if (!AUTH0_ENABLED) {
    return NextResponse.next();
  }

  // Check for the auth0 v4 session cookie (__session)
  // If it's missing, the user is definitely not logged in — send to login
  const sessionCookie =
    request.cookies.get("__session") ??
    request.cookies.get("__session.0"); // chunked cookie fallback

  if (!sessionCookie) {
    const loginUrl = new URL("/auth/login", request.url);
    loginUrl.searchParams.set("returnTo", pathname);
    return NextResponse.redirect(loginUrl);
  }

  // Cookie present — let auth0 middleware handle token refresh etc.
  return await auth0.middleware(request);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|icon\\.png|favicon\\.ico).*)"],
};
