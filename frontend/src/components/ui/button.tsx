import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#4f7c9c]/30 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-[#4f7c9c] text-white shadow-[0_8px_22px_rgba(79,124,156,.26)] hover:bg-[#416b89]",
        outline: "border border-[#dfe7eb] bg-white/90 text-[#4f7c9c] shadow-[0_8px_24px_rgba(55,86,108,.07)] hover:bg-[#f5f8f9]",
        ghost: "text-[#53688f] hover:bg-[#f2f5fb] hover:text-[#15356d]",
      },
      size: {
        default: "h-11 px-4",
        sm: "h-9 rounded-lg px-3",
        icon: "size-11",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export function Button({
  className,
  variant,
  size,
  ...props
}: React.ComponentProps<"button"> & VariantProps<typeof buttonVariants>) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
