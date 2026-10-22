import { redirect } from "next/navigation";

import { AdminActions } from "@/components/admin-actions";
import { AdminDashboard } from "@/components/admin-dashboard";
import { AdminHeader } from "@/components/admin-header";
import { currentAdmin } from "@/lib/admin-auth";
import { listCategories, listJobs } from "@/lib/admin-storage";

export default async function AdminPage() {
  const admin = await currentAdmin();
  if (!admin) redirect("/admin/login");
  const [categories, uploads] = await Promise.all([listCategories(), listJobs()]);
  return (
    <div className="relative min-h-dvh overflow-x-hidden">
      <div className="pointer-events-none fixed inset-0 -z-10 bg-[radial-gradient(circle_at_12%_5%,rgba(230,238,242,.78),transparent_25%),radial-gradient(circle_at_90%_12%,rgba(235,242,245,.75),transparent_26%),linear-gradient(180deg,#fbfcfd_0%,#ffffff_48%,#f8fbfc_100%)]" />
      <AdminHeader action={<AdminActions current="documents" />} />
      <AdminDashboard
        initialCategories={categories}
        initialJobs={uploads.jobs}
        initialUpdate={uploads.update}
      />
    </div>
  );
}
