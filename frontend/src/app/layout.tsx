import type { Metadata } from "next";

import "@fontsource-variable/manrope";
import "./globals.css";

export const metadata: Metadata = {
  title: "База знаний",
  description: "Ответы по внутренним документам компании",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
