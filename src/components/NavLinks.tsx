"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/alerts", label: "Web Alerts" },
  { href: "/document-extractions", label: "Document Extractions" },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
    <>
      {links.map(({ href, label }) => {
        const active = pathname.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            className={
              active
                ? "text-sm font-semibold text-gray-900 border-b-2 border-gray-900 pb-0.5"
                : "text-sm font-medium text-gray-500 hover:text-gray-900"
            }
          >
            {label}
          </Link>
        );
      })}
    </>
  );
}
