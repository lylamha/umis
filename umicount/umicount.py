#!/usr/bin/env python

import sys
import HTSeq
import itertools
import collections
import pandas
import copy
import re
from re import findall
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import json
import gzip

"""
umicount: Tools for processing unique molecular identifiers of single-cell RNA-sequencing
"""

def highquality_read(read1, read2, cbs, cbe, mbs, mbe, minqual):
    if read1.qual[cbs-1:cbe].min() >= minqual and read1.qual[mbs-1:mbe].min() >= minqual:
        return True
    else:
        return False

"""
Tools for processing cell and molecular barcodes of reads in the FASTQ file.
"""

def fastq_trim(args):
    fastq_file1 = HTSeq.FastqReader(args.fastq1)
    fastq_file2 = HTSeq.FastqReader(args.fastq2)
    fastq_out = open(args.outfastq, "w")
    for read1, read2 in itertools.izip(fastq_file1, fastq_file2):
        if highquality_read(read1, read2, args.cbs, args.cbe, args.mbs, args.mbe, args.minqual):            
            read1.name = read1.name.split()[0]
            read2.name = read2.name.split()[0]
            if read1.name != read2.name:
                sys.exit("read1 name does not match read2 name")
                
            read2.name = "{name}:CELL_{cell}:UMI_{umi}".format(
                name=read2.name,
                cell=read1.seq[args.cbs-1:args.cbe],
                umi=read1.seq[args.mbs-1:args.mbe])
            
            read2.write_to_fastq_file(fastq_out)


def stream_fastq(file_handler):
    ''' Generator which gives all four lines if a fastq read as one string
    '''
    next_element = ''
    for i, line in enumerate(file_handler):
        next_element += line
        if i % 4 == 3:
            yield next_element
            next_element =''


def fastq_transform(args):
    ''' Transform input reads to the umicount compatible read layout using regular expressions
    as defined in a transform file. [To be described]
    '''
    read_template = '{name}:CELL_{CB}:UMI_{MB}\n{seq}\n+\n{qual}\n'

    transform = json.load(open(args.transform))
    read1_regex = re.compile(transform['read1'])
    read2_regex = re.compile(transform['read2']) if args.fastq2 else None

    fastq1_fh = open(args.fastq1)
    if args.fastq1.endswith('gz'):
        fastq1_fh = gzip.GzipFile(fileobj=fastq1_fh)

    fastq_file1 = stream_fastq(fastq1_fh)

    if args.fastq2:
        fastq2_fh = open(args.fastq2)
        if args.fastq2.endswith('gz'):
            fastq2_fh = gzip.GzipFile(fileobj=fastq2_fh)

        fastq_file2 = stream_fastq(fastq2_fh)

    else:
        fastq_file2 = itertools.cycle((None,))

    fastq_out = open(args.outfastq, "w")
    for read1, read2 in itertools.izip(fastq_file1, fastq_file2):
        # Parse the reads with the regexes
        read1_match = read1_regex.search(read1)
        if not read1_match:
            continue

        read1_dict = read1_match.groupdict()

        if args.fastq2:
            read2_match = read2_regex.search(read2)
            if not read2_match:
                continue

            read2_dict = read2_match.groupdict()

        else:
            read2_dict = dict()

        read1_dict.update(read2_dict)

        if args.demuxed_cb:
            read1_dict['CB'] = args.demuxed_cb

        fastq_out.write(read_template.format(**read1_dict))

    fastq_out.close()


def extract_cellbarcode(rname):
    return findall("CELL_(\w*):", rname)[0].strip()

def extract_umi(rname):
    return findall("UMI_(\w*)", rname)[0].strip()

def check_exon_overlap(iv, exons):
    iset = None
    for iv2, step_set in exons[iv].steps():
        if iset is None:
            iset = step_set.copy()
        else:
            iset.update(step_set)
    return iset

"""
Tools for making a UMI count table (genes by cells) in the single-merged SAM file
"""

def tag_count(args):
    ''' Count up evidence for tagged molecules
    '''
    from hts import Bam

    sam_file = Bam(args.sam)

    gene_map = None
    if args.geneMap:
        with open(args.geneMap) as fh:
            gene_map = dict(p.strip().split() for p in fh)

    parser_re = re.compile('(.*):CELL_(?P<CB>.*):UMI_(?P<MB>.*)')

    evidence = collections.defaultdict(int)

    for i, aln in enumerate(sam_file):
        if aln.tname:
            match = parser_re.search(aln.qname).groupdict()
            CB = match['CB']
            MB = match['MB']

            if gene_map:
                target_name = gene_map[aln.tname]
            else:
                target_name = aln.tname

            if args.positional:
                e_tuple = (CB, target_name, aln.pos, MB)
            else:
                e_tuple = (CB, target_name, MB)
            
            # TODO: Parsing NH should be more robust.
            nh = float(aln.aux[-1][-1])  # Number of hits per read
            evidence[e_tuple] += 1. / nh

    with open(args.out, 'w') as out_fh:
        for key in evidence:
            line = ','.join(map(str, key)) + ',' + str(evidence[key]) + '\n'
            out_fh.write(line)


def sam_spike_count(sam_file, cell_barcodes, gene_cell_umi_sets, gene_umi_sets, minaqual, umilen):
    for aln in sam_file:
        if aln.aligned and aln.aQual >= minaqual:
            umi = extract_umi(aln.read.name)
            if umilen is not None:
                umi = umi[:umilen]
            cell = extract_cellbarcode(aln.read.name)
            if cell in cell_barcodes:
                if cell not in gene_cell_umi_sets:
                    gene_cell_umi_sets[cell] = copy.deepcopy(gene_umi_sets)
                gene_cell_umi_sets[cell][aln.iv.chrom].add(umi)

    gene_cell_counts = collections.defaultdict(dict)
    for cell in gene_cell_umi_sets:
        for gene in gene_cell_umi_sets[cell]:
            gene_cell_counts[cell][gene] = len(gene_cell_umi_sets[cell][gene])
    
    return gene_cell_counts

def sam_count(args):
    sam_file = HTSeq.SAM_Reader(args.sam)
    gtf_file = HTSeq.GFF_Reader(args.gtf)

    with open(args.cell, "r") as cell_file:
        lines = cell_file.readlines()
    cell_barcodes = [line.strip() for line in lines]

    exons = HTSeq.GenomicArrayOfSets("auto", stranded=False)
    
    gene_counts = {}
    gene_umi_sets = {}
    for feature in gtf_file:
        if feature.type == "exon":
            exons[feature.iv] += feature.name
            gene_counts[feature.name] = 0
            gene_umi_sets[feature.name] = set()
 
    gene_cell_umi_sets = collections.defaultdict(dict)
    if args.spike == True:
        gene_cell_counts = sam_spike_count(sam_file, cell_barcodes, gene_cell_umi_sets, gene_umi_sets, args.minqual, args.umilen)

#    for aln in sam_file:
#        if aln.aligned:
#            umi = extract_umi(aln.read.name)
#            cell = extract_cellbarcode(aln.read.name)
#            if cell not in gene_cell_counts:
#                gene_cell_counts[cell] = gene_counts.copy()
#            iset = check_exon_overlap(aln.iv, exons)
#            if len(iset) == 1:
#                print "Couting: CELL:{cell} Gene:{gene}".format(cell=cell, gene=list(iset)[0])
#                gene_cell_counts[cell][list(iset)[0]] += 1
                
        gene_cell_table = pandas.DataFrame(gene_cell_counts)
        gene_cell_table.fillna(value=0)
        gene_cell_table.to_csv(args.outcount, sep="\t")

def cell_count(args):
    fastq_file = HTSeq.FastqReader(args.fastq)
    cell_counts = collections.Counter()
    for read in fastq_file:
        if  read.qual[args.cbs-1:args.cbe].min() >= args.minqual:
            read.name = read.name.split()[0]
            cell_counts[read.seq[args.cbs-1:args.cbe]] += 1
            
    for cell_barcode, barcode_count  in cell_counts.most_common():
        print "{key}\t{val}".format(key=cell_barcode, val=barcode_count)


def main():
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(help="subcommad help")

    subparser_fastqtransform = subparsers.add_parser("fastqtransform", description="Reformat fastq reads to umicount compatible format",
                                                                  formatter_class=ArgumentDefaultsHelpFormatter,
                                                                  help="trim cell and molecular barcodes and incorporate them into read name")
    subparser_fastqtransform.add_argument("--fastq1", metavar="FASTQ1", help="input FASTQ file 1", required=True)
    subparser_fastqtransform.add_argument("--fastq2", metavar="FASTQ1", help="input FASTQ file 2 for paired-end reads", required=False)
    subparser_fastqtransform.add_argument("--transform", metavar="TRANSFORM", help="FASTQ Transform JSON file", required=True)
    subparser_fastqtransform.add_argument("--outfastq", metavar="FASTQOUT", help="output FASTQ file for FASTQ1", required=True)
    subparser_fastqtransform.add_argument("--demuxed_cb", metavar="DEMUXED_CB", help="Set CB value to this in the transformed read name. Use this if your files have already been demultiplexed (e.g. STRT-Seq).", required=False)
    subparser_fastqtransform.set_defaults(func=fastq_transform)

    subparser_fastqtrim = subparsers.add_parser("fastqtrim", description="Trim the cell and molecular barcodes from the read", formatter_class=ArgumentDefaultsHelpFormatter, help="trim cell and molecular barcodes and incorporate them into read name")
    subparser_fastqtrim.add_argument("--fastq1", metavar="FASTQ1", help="input FASTQ file 1", required=True)
    subparser_fastqtrim.add_argument("--fastq2", metavar="FASTQ1", help="input FASTQ file 2 for paired-end reads", required=True)
    subparser_fastqtrim.add_argument("--outfastq", metavar="FASTQOUT", help="output FASTQ file for FASTQ1", required=True)
    subparser_fastqtrim.add_argument("--cbs", metavar="CELLBARCODESTART", help="start position of cell barcode, e.g. 1", required=True, type=int)
    subparser_fastqtrim.add_argument("--cbe", metavar="CELLBARCODEEND", help="end position of cell barcode, e.g. 12", required=True, type=int)
    subparser_fastqtrim.add_argument("--mbs", metavar="UMISTART", help="start position of molecular barcode (UMI), e.g. 13", required=True, type=int)
    subparser_fastqtrim.add_argument("--mbe", metavar="UMIEND", help="end position of molecular barcode (UMI), e.g. 20", required=True, type=int)
    subparser_fastqtrim.add_argument("--minqual", metavar="MINQUAL", help="remove all reads with the minimum Phred quality score within the cell and molecular barcodes lower than the given minimum value", default=10, type=int)
    subparser_fastqtrim.set_defaults(func=fastq_trim)

    subparser_tagcount = subparsers.add_parser("tagcount", description="Count tag evidence from the SAM file",
                                                        formatter_class=ArgumentDefaultsHelpFormatter,
                                                        help="count reads from the SAM file")
    subparser_tagcount.add_argument("--sam", metavar="SAM", help="SAM file", required=True)
    subparser_tagcount.add_argument("--geneMap", "-g", metavar="GENEMAP",
                                                       help="Mapping of transcripts to genes", required=False)
    subparser_tagcount.add_argument("--positional", help="Consider position in transcript as molecular evidence",
                                                    required=False, action='store_true')
    subparser_tagcount.add_argument("--out", metavar="OUT", help="Output file", required=True)
    subparser_tagcount.set_defaults(func=tag_count)

    subparser_samcount = subparsers.add_parser("samcount", description="Count reads from the SAM file", formatter_class=ArgumentDefaultsHelpFormatter, help="count reads from the SAM file")
    subparser_samcount.add_argument("--sam", metavar="SAM", help="SAM file", required=True)
    subparser_samcount.add_argument("--gtf", metavar="GTF", help="GTF file", required=True)
    subparser_samcount.add_argument("--cell", metavar="CELL", help="Cell barcode file", required=True)
    subparser_samcount.add_argument("--spike", help="Are they spike-ins?", action="store_true")
    subparser_samcount.add_argument("--minqual", metavar="MINQUAL", help="remove all reads with the alignment quality lower than the given minimum value", default=10, type=int)
    subparser_samcount.add_argument("--outcount", metavar="COUNTTXT", help="write a UMI count table", required=True)
    subparser_samcount.add_argument("--umilen", metavar="UMILENGTH", help="UMI length", type=int)
    subparser_samcount.set_defaults(func=sam_count)

    subparser_cellcount = subparsers.add_parser("cellcount", description="Count cell barcodes from the FASTQ file", formatter_class=ArgumentDefaultsHelpFormatter, help="count cell barcodes from the FASTQ file")
    subparser_cellcount.add_argument("--fastq", metavar="FASTQ", help="input FASTQ file with the cell barcode", required=True)
    subparser_cellcount.add_argument("--cbs", metavar="CELLBARCODESTART", help="start position of cell barcode, e.g. 1", required=True, type=int)
    subparser_cellcount.add_argument("--cbe", metavar="CELLBARCODEEND", help="end position of cell barcode, e.g. 12", required=True, type=int)
    subparser_cellcount.add_argument("--minqual", metavar="MINQUAL", help="remove all reads with the minimum Phred quality score within the cell and molecular barcodes  lower than the given minimum value", default=10, type=int)
    subparser_cellcount.set_defaults(func=cell_count)

    
    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()