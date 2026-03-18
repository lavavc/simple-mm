import fs from 'fs';
import path from 'path';
import { notFound } from 'next/navigation';
import matter from 'gray-matter';
import { DocContent } from '../page';

const DOCS_DIR = path.join(process.cwd(), 'docs');

export async function generateStaticParams() {
  const params: { slug: string[] }[] = [];

  for (const f of fs.readdirSync(DOCS_DIR)) {
    const fullPath = path.join(DOCS_DIR, f);
    const stat = fs.statSync(fullPath);

    if (stat.isFile() && f.endsWith('.md') && f !== 'index.md' && !f.startsWith('_')) {
      params.push({ slug: [f.replace(/\.md$/, '')] });
    } else if (stat.isDirectory() && f !== 'archive') {
      for (const sub of fs.readdirSync(fullPath)) {
        if (sub.endsWith('.md') && !sub.startsWith('_')) {
          params.push({ slug: [f, sub.replace(/\.md$/, '')] });
        }
      }
    }
  }

  return params;
}

export default async function DocPage({ params }: { params: Promise<{ slug: string[] }> }) {
  const { slug } = await params;
  const filePath = path.join(DOCS_DIR, ...slug) + '.md';

  if (!fs.existsSync(filePath)) notFound();

  const { content } = matter(fs.readFileSync(filePath, 'utf-8'));

  return <DocContent content={content} />;
}
