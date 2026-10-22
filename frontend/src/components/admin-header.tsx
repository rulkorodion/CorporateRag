import Link from "next/link";

import { BrandMark } from "@/components/brand-mark";

export function AdminHeader({ action }: { action?: React.ReactNode }) {
  return (
    <header className="flex w-full items-center justify-between gap-4 px-4 pt-4 sm:px-6 sm:pt-5">
      <Link href="/" className="flex items-center gap-2.5" aria-label="База знаний">
        <BrandMark compact />
        <p className="text-[21px] font-semibold tracking-[-.025em] text-[#294c64] sm:text-[23px]">База знаний</p>
      </Link>
      {action}
    </header>
  );
}
