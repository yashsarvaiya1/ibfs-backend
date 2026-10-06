/* Measure after fonts/images load. Every physical sheet owns its header/footer;
   reserve the final item with the summary rather than emitting a totals-only page. */
() => {
  const height = node => node.getBoundingClientRect().height;
  for (const source of [...document.querySelectorAll('[data-source-document]')]) {
    const pages = [];
    const summarySource = source.querySelector('.page-summary');
    const summaryHeight = summarySource ? height(summarySource) : 0;
    const newPage = () => {
      const page = source.cloneNode(true);
      page.removeAttribute('data-source-document');
      // New image elements may not expose intrinsic dimensions immediately.
      // Freeze their already-decoded source dimensions before measuring space.
      const sourceImages = [...source.querySelectorAll('img')];
      page.querySelectorAll('img').forEach((image, index) => {
        const bounds = sourceImages[index].getBoundingClientRect();
        image.style.width = `${bounds.width}px`;
        image.style.height = `${bounds.height}px`;
      });
      const body = page.querySelector('.page-body');
      body.replaceChildren();
      const summary = page.querySelector('.page-summary');
      if (summary) {
        summary.style.height = `${summaryHeight}px`;
        summary.querySelectorAll('[data-summary-value]').forEach(value => { value.textContent = ''; });
      }
      source.before(page);
      pages.push(page);
      return { page, body };
    };
    const tableSource = source.querySelector('.items');
    const end = source.querySelector('.document-end').cloneNode(true);
    let current = newPage();
    const capacity = height(current.body) - 2; // rounding allowance at print resolution
    if (capacity < 100) throw new Error('Letterhead and document header leave too little space. Reduce the header reservation or address length.');
    const makeTable = body => {
      const table = tableSource.cloneNode(true);
      table.querySelector('tbody').replaceChildren();
      body.append(table);
      return table;
    };
    const fits = () => {
      const last = current.body.lastElementChild;
      return !last || last.getBoundingClientRect().bottom <= current.body.getBoundingClientRect().top + capacity;
    };
    let rows = tableSource ? [...tableSource.querySelectorAll('tbody > tr')].map(row => row.cloneNode(true)) : [];
    current.body.append(end);
    const notesHeight = height(end);
    const notes = [];
    // Unusually long saved terms/notes may need continuation sheets. Keep all
    // their text and retain the final item for the sheet carrying the total.
    const finalRowHeight = tableSource ? height(tableSource.querySelector('tbody > tr:last-child')) : 0;
    const sourceHeadingHeight = tableSource ? height(tableSource.querySelector('thead')) : 0;
    if (notesHeight + finalRowHeight + sourceHeadingHeight + 4 > capacity) {
      for (const block of [...end.querySelector('.end-notes').children]) {
        notes.push(block);
        block.remove();
      }
    }
    const endHeight = height(end);
    end.remove();
    if (endHeight > capacity) throw new Error('The total breakdown exceeds one page. Reduce the number of separate charge or tax lines.');
    const measure = tableSource ? makeTable(current.body) : null;
    const tbody = measure?.querySelector('tbody');
    const headingHeight = measure ? height(measure) : 0;
    // Split a very long description instead of clipping it. Quantities/rates/
    // amounts remain on its final fragment, so their values are printed once.
    const splitRow = (row, limit) => {
      tbody.replaceChildren(row);
      if (height(row) <= limit) return [row];
      const detail = row.children[1];
      const name = detail.querySelector('.item-name');
      const savedDescription = detail.querySelector('.item-description');
      const text = savedDescription ? savedDescription.textContent : detail.innerText;
      const words = (text.match(/\S+\s*/g) || []).flatMap(word => word.match(/[\s\S]{1,128}/g));
      const fragments = [];
      let start = 0;
      while (start < words.length) {
        const fragment = row.cloneNode(true);
        for (const cell of [...fragment.children].filter((_, i) => i !== 1)) cell.textContent = '';
        const cell = fragment.children[1];
        cell.replaceChildren();
        if (savedDescription && name) cell.append(name.cloneNode(true));
        const description = document.createElement('div');
        description.className = 'item-description';
        cell.append(description);
        tbody.replaceChildren(fragment);
        let low = 1, high = words.length - start, count = 0;
        while (low <= high) {
          const middle = Math.floor((low + high) / 2);
          description.textContent = words.slice(start, start + middle).join('');
          if (height(fragment) <= limit) { count = middle; low = middle + 1; }
          else high = middle - 1;
        }
        if (!count) throw new Error('An item cannot fit below the document header. Reduce the header space.');
        description.textContent = words.slice(start, start + count).join('');
        fragments.push(fragment);
        start += count;
      }
      const last = fragments.at(-1);
      [...row.children].forEach((cell, i) => { if (i !== 1) last.children[i].replaceChildren(...[...cell.childNodes].map(n => n.cloneNode(true))); });
      return fragments;
    };
    const rowLimit = capacity - headingHeight - 4;
    rows = rows.flatMap((row, i) => splitRow(row, Math.min(rowLimit,
      capacity - headingHeight - (i === rows.length - 1 ? endHeight : 0) - 4)));
    measure?.remove();
    let position = 0;
    while (position < rows.length) {
      const table = makeTable(current.body);
      const target = table.querySelector('tbody');
      const start = position;
      while (position < rows.length) {
        const row = rows[position];
        target.append(row);
        if (!fits()) { row.remove(); break; }
        position++;
      }
      if (position === rows.length && !notes.length) {
        current.body.append(end);
        if (fits()) break;
        end.remove();
      }
      // Leave the last item on the final sheet together with the summary.
      if (position === rows.length) {
        target.lastElementChild.remove();
        position--;
      }
      if (position === start) {
        table.remove();
        break; // final item/summary will be placed below
      }
      current = newPage();
    }
    // Saved prose is usually part of the summary. Split only oversized prose,
    // preserving it before the reserved final item and amount.
    for (const note of notes) {
      const text = note.querySelector('.note-text').textContent;
      const words = (text.match(/\S+\s*/g) || []).flatMap(word => word.match(/[\s\S]{1,128}/g));
      let start = 0;
      while (start < words.length) {
        const block = note.cloneNode(true);
        const paragraph = block.querySelector('.note-text');
        current.body.append(block);
        let low = 1, high = words.length - start, count = 0;
        while (low <= high) {
          const middle = Math.floor((low + high) / 2);
          paragraph.textContent = words.slice(start, start + middle).join('');
          if (fits()) { count = middle; low = middle + 1; } else high = middle - 1;
        }
        if (!count) { block.remove(); current = newPage(); continue; }
        paragraph.textContent = words.slice(start, start + count).join('');
        start += count;
        if (start < words.length) current = newPage();
      }
    }
    if (!end.isConnected) {
      let table;
      if (position < rows.length) {
        table = makeTable(current.body);
        table.querySelector('tbody').append(...rows.slice(position));
      }
      current.body.append(end);
      if (!fits()) {
        if (table) table.remove();
        end.remove();
        current = newPage();
        if (position < rows.length) makeTable(current.body).querySelector('tbody').append(...rows.slice(position));
        current.body.append(end);
      }
      if (!fits()) throw new Error('The last item and total cannot fit together. Reduce the header space or total breakdown.');
    }
    // Drop an unused measurement sheet (e.g. one oversized first item).
    for (const page of [...pages]) {
      if (!page.querySelector('.page-body').children.length) { page.remove(); pages.splice(pages.indexOf(page), 1); }
    }
    if (summarySource) {
      const summary = summarySource.cloneNode(true);
      summary.style.height = `${summaryHeight}px`;
      pages.at(-1).querySelector('.page-summary').replaceWith(summary);
      pages.at(-1).setAttribute('data-final-page', 'true');
    }
    pages.forEach((page, index) => { page.querySelector('.page-count').textContent = `Page ${index + 1} of ${pages.length}`; });
    source.remove();
  }
}
