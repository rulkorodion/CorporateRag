"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogTitle = DialogPrimitive.Title;
export const DialogDescription = DialogPrimitive.Description;

export function DialogContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-[#102757]/20 backdrop-blur-[5px] data-[state=closed]:animate-out data-[state=open]:animate-in" />
      <DialogPrimitive.Content
        className={cn(
          "fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-[520px] -translate-x-1/2 -translate-y-1/2 rounded-[26px] border border-white/80 bg-white p-7 text-[#17366e] shadow-[0_24px_80px_rgba(20,47,100,.2)] outline-none sm:p-9",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close className="absolute right-5 top-5 grid size-9 place-items-center rounded-full text-[#6f8392] transition hover:bg-[#eef3f5] hover:text-[#294c64] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#4f7c9c]/30">
          <X className="size-5" />
          <span className="sr-only">Закрыть</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
