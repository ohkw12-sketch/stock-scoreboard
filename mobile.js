(() => {
  const cleanLabel = value => String(value || '')
    .replace(/[↕↑↓]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  function headerLabels(table) {
    const rows = Array.from(table.tHead?.rows || []);
    if (!rows.length) return [];

    const grid = [];
    rows.forEach((row, rowIndex) => {
      grid[rowIndex] ||= [];
      let column = 0;
      Array.from(row.cells).forEach(cell => {
        while (grid[rowIndex][column]) column += 1;
        const label = cleanLabel(cell.textContent);
        const rowSpan = Math.max(1, Number(cell.rowSpan) || 1);
        const colSpan = Math.max(1, Number(cell.colSpan) || 1);
        for (let rowOffset = 0; rowOffset < rowSpan; rowOffset += 1) {
          grid[rowIndex + rowOffset] ||= [];
          for (let colOffset = 0; colOffset < colSpan; colOffset += 1) {
            const labels = grid[rowIndex + rowOffset][column + colOffset] || [];
            if (label && !labels.includes(label)) labels.push(label);
            grid[rowIndex + rowOffset][column + colOffset] = labels;
          }
        }
        column += colSpan;
      });
    });

    const columnCount = Math.max(0, ...grid.map(row => row.length));
    return Array.from({ length: columnCount }, (_, columnIndex) => {
      const path = [];
      grid.forEach(row => {
        (row[columnIndex] || []).forEach(label => {
          if (label && !path.includes(label)) path.push(label);
        });
      });
      return path.join(' · ');
    });
  }

  function labelTable(table) {
    table.classList.add('mobile-card-table');
    const labels = headerLabels(table);
    Array.from(table.tBodies || []).forEach(body => {
      Array.from(body.rows).forEach(row => {
        const cells = Array.from(row.cells);
        const isEmptyRow = cells.length === 1 && Number(cells[0].colSpan) > 1;
        row.classList.toggle('mobile-empty-row', isEmptyRow);
        cells.forEach((cell, index) => {
          if (isEmptyRow) cell.removeAttribute('data-label');
          else cell.dataset.label = labels[index] || `항목 ${index + 1}`;
        });
      });
    });
  }

  function refreshMobileTables(root = document) {
    root.querySelectorAll('.tablewrap table, .issue-tablewrap table').forEach(labelTable);
  }

  function initializeMobileLayout() {
    const shell = document.querySelector('.shell');
    refreshMobileTables();
    if (!shell || typeof MutationObserver === 'undefined') return;

    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        refreshMobileTables(shell);
      });
    });
    observer.observe(shell, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeMobileLayout, { once: true });
  } else {
    initializeMobileLayout();
  }
})();
