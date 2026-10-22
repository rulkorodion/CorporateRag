import { BarChart3, Files } from "lucide-react";
import Link from "next/link";

import { AdminLogoutButton } from "@/components/admin-logout-button";

export function AdminActions({ current }: { current: "documents" | "statistics" }) {
  const target = current === "documents"
    ? { href: "/admin/statistics", label: "Статистика", icon: BarChart3 }
    : { href: "/admin", label: "Документы", icon: Files };
  const Icon = target.icon;
  return (
    <div className="flex items-center gap-2">
      <Link
        href={target.href}
        className="inline-flex h-[42px] items-center justify-center gap-2 rounded-[25px] border border-[#dfe7eb] bg-white/90 px-4 text-[13px] font-medium text-[#4f7c9c] shadow-[0_8px_24px_rgba(55,86,108,.07)] transition-colors hover:bg-[#f5f8f9] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#4f7c9c]/30"
      >
        <Icon className="size-[17px]" />
        <span className="hidden sm:inline">{target.label}</span>
      </Link>
      <AdminLogoutButton />
    </div>
  );
}
