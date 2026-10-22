import { redirect } from "next/navigation";

import { AdminHeader } from "@/components/admin-header";
import { AdminLoginForm } from "@/components/admin-login-form";
import { currentAdmin } from "@/lib/admin-auth";

export default async function AdminLoginPage() {
  if (await currentAdmin()) redirect("/admin");
  return (
    <div className="relative flex min-h-dvh flex-col overflow-hidden">
      <div className="pointer-events-none fixed inset-0 -z-10 bg-[radial-gradient(circle_at_15%_4%,rgba(230,238,242,.8),transparent_27%),radial-gradient(circle_at_85%_20%,rgba(231,239,243,.75),transparent_28%),linear-gradient(180deg,#fbfcfd_0%,#ffffff_62%,#f8fbfc_100%)]" />
      <AdminHeader />
      <main className="grid flex-1 place-items-center px-5 py-12 sm:py-16">
        <AdminLoginForm />
      </main>
    </div>
  );
}

