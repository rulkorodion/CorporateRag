"use client";

import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";

export function AdminLogoutButton() {
  const router = useRouter();

  async function logout() {
    await fetch("/api/admin/logout", { method: "POST" });
    router.replace("/");
    router.refresh();
  }

  return (
    <Button
      variant="outline"
      className="h-[42px] rounded-[25px] px-4 text-[13px]"
      onClick={logout}
    >
      <LogOut className="size-[18px]" />
      Выйти
    </Button>
  );
}
