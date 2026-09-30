import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
} from "@tanstack/react-table";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

export interface DataTableProps<TData> {
  columns: ColumnDef<TData>[];
  data: TData[];
  density?: "compact" | "default";
  getRowId?: (row: TData) => string;
  onSelectionChange?: (selected: ReadonlySet<string>) => void;
  selectedRowIds?: ReadonlySet<string>;
}

export function DataTable<TData>({
  columns,
  data,
  density = "default",
  getRowId,
  onSelectionChange,
  selectedRowIds = new Set<string>(),
}: DataTableProps<TData>) {
  const table = useReactTable({
    columns,
    data,
    getCoreRowModel: getCoreRowModel(),
    ...(getRowId === undefined ? {} : { getRowId }),
  });
  const visibleIds = table.getRowModel().rows.map((row) => row.id);
  const allVisibleSelected =
    visibleIds.length > 0 && visibleIds.every((id) => selectedRowIds.has(id));

  const updateSelection = (id: string, selected: boolean) => {
    const next = new Set(selectedRowIds);
    if (selected) next.add(id);
    else next.delete(id);
    onSelectionChange?.(next);
  };

  return (
    <div className="overflow-hidden rounded-lg border">
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {onSelectionChange === undefined ? null : (
                <TableHead className="w-10">
                  <input
                    aria-label="Выбрать все строки на странице"
                    checked={allVisibleSelected}
                    className="accent-primary size-4"
                    type="checkbox"
                    onChange={(event) => {
                      const next = new Set(selectedRowIds);
                      for (const id of visibleIds) {
                        if (event.target.checked) next.add(id);
                        else next.delete(id);
                      }
                      onSelectionChange(next);
                    }}
                  />
                </TableHead>
              )}
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id}>
                  {header.isPlaceholder
                    ? null
                    : flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.map((row) => (
            <TableRow
              data-state={selectedRowIds.has(row.id) ? "selected" : undefined}
              key={row.id}
            >
              {onSelectionChange === undefined ? null : (
                <TableCell>
                  <input
                    aria-label={`Выбрать строку ${row.id}`}
                    checked={selectedRowIds.has(row.id)}
                    className="accent-primary size-4"
                    type="checkbox"
                    onChange={(event) => {
                      updateSelection(row.id, event.target.checked);
                    }}
                  />
                </TableCell>
              )}
              {row.getVisibleCells().map((cell) => (
                <TableCell
                  className={density === "compact" ? "py-1" : "py-2"}
                  key={cell.id}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function NumberCell({ value }: { value: number }) {
  return (
    <span className="block text-right font-mono tabular-nums">
      {new Intl.NumberFormat("ru-RU").format(value)}
    </span>
  );
}
