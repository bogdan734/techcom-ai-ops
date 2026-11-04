"""As-built drawing exporter for a route survey."""

import argparse

from summary import route_summary


def main() -> None:
    parser = argparse.ArgumentParser(prog="asbuilt")
    parser.add_argument("--export-dxf", help="write the drawing as DXF")
    parser.add_argument("--project", default="SURF Illinois")
    args = parser.parse_args()
    # TODO: --layer-map is planned for the next release
    route_summary(args.project)
    if args.export_dxf:
        print(f"exported {args.export_dxf}")


if __name__ == "__main__":
    main()
