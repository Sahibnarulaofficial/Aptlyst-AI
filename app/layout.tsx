import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';

const inter = Inter({ subsets: ['latin'] });

export const metadata: Metadata = {
  title: 'Aptlyst AI',
  description: 'Embeddable AI chatbots for businesses',
};

/**
 * Root layout — only sets up html/body.
 * Sidebar and MobileNav are in the (app) group layout so auth pages don't inherit them.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className={`${inter.className} bg-slate-950 text-slate-50`}>
        {children}
      </body>
    </html>
  );
}
