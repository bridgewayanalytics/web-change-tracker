import type { Metadata } from "next";
import { auth0 } from "@/lib/auth0";
import { NavLinks } from "@/components/NavLinks";
import "./globals.css";

export const metadata: Metadata = {
  title: "NAIC Web Change Alerts",
  description: "Web change alerts dashboard",
  icons: {
    icon: "/icon.png",
  },
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const session = process.env.AUTH0_SECRET
    ? await auth0.getSession().catch(() => null)
    : null;
  const user = session?.user;

  return (
    <html lang="en">
      <body className="antialiased">
        <nav className="border-b border-gray-200 bg-white px-8 py-3 flex items-center gap-6">
          <NavLinks />
          <div className="ml-auto flex items-center gap-4">
            {user && (
              <span className="text-xs text-gray-500">{user.email ?? user.name}</span>
            )}
            {process.env.AUTH0_SECRET && (
              <a href="/auth/logout" className="text-xs font-medium text-gray-500 hover:text-gray-900" rel="nofollow">
                Sign out
              </a>
            )}
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
