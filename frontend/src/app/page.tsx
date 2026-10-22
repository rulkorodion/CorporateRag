import { AboutDialog } from "@/components/about-dialog";
import { BrandMark } from "@/components/brand-mark";
import { Chat } from "@/components/chat";

export default function Home() {
  return (
    <div className="relative flex min-h-dvh flex-col overflow-x-hidden">
      <div className="pointer-events-none fixed inset-0 -z-10 bg-[radial-gradient(circle_at_15%_4%,rgba(230,238,242,.75),transparent_24%),radial-gradient(circle_at_86%_16%,rgba(237,243,246,.62),transparent_25%),linear-gradient(180deg,#fbfcfd_0%,#ffffff_44%,#fafcfd_100%)]" />
      <header className="flex w-full items-start justify-between gap-5 px-3 pt-3 sm:px-5 sm:pt-4 lg:px-6 lg:pt-5">
        <div className="flex items-center gap-2.5">
          <BrandMark compact />
          <h1 className="text-[21px] font-semibold tracking-[-.025em] text-[#294c64] sm:text-[23px]">База знаний</h1>
        </div>
        <AboutDialog />
      </header>
      <Chat />
    </div>
  );
}
