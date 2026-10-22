import Image from "next/image";

export function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div
      className={
        compact
          ? "size-12 overflow-hidden"
          : "size-[52px] overflow-hidden max-sm:size-12"
      }
      aria-hidden="true"
    >
      <Image
        src="/icons/knowledge-base-ktz.png"
        alt=""
        width={512}
        height={512}
        className="size-full scale-[1.55] object-contain"
        priority
      />
    </div>
  );
}
